/** @odoo-module **/

/**
 * Production Flow Tab — widget field hiển thị dashboard MO con của Master MO.
 *
 * Luồng tự động:
 *   1. Start WO "Quản lý" → auto-start WO đầu của từng Child MO (song song)
 *   2. WO trong Child MO chạy tuần tự
 *   3. Tất cả Child WO Done → auto-start "Gia công"
 *   4. Gia công Done → auto-start "Đóng gói"
 *   5. Đóng gói Done → auto-finish "Quản lý"
 *
 * Data source: action_get_production_flow_data() — single RPC, trả về toàn bộ payload.
 *
 * Timer live:
 *   - WO đang "progress": base (từ server, tính đến lúc load) + elapsed (client, mỗi giây)
 *   - WO không chạy: dùng duration tĩnh
 */

import { Component, useState, onWillStart, onWillDestroy } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

// ─────────────────────────────────────────────────────────────────────────────
// Hàm tiện ích
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Chuyển số phút sang chuỗi thời gian.
 * showSeconds=true  → "HH:MM:SS"
 * showSeconds=false → "HH:MM"
 */
function formatDuration(minutes, showSeconds) {
    if (!minutes || minutes < 0) return showSeconds ? "00:00:00" : "00:00";
    const totalSec = Math.floor(minutes * 60);
    const h  = Math.floor(totalSec / 3600);
    const m  = Math.floor((totalSec % 3600) / 60);
    const s  = totalSec % 60;
    const hh = String(h).padStart(2, "0");
    const mm = String(m).padStart(2, "0");
    const ss = String(s).padStart(2, "0");
    return showSeconds ? `${hh}:${mm}:${ss}` : `${hh}:${mm}`;
}

/**
 * isRunning: truyền wo.is_running để phân biệt "progress đang chạy" vs "progress đã tạm dừng".
 * Mặc định true để backward-compat với MO-level state string.
 */
function getStatusClass(state, isRunning = true) {
    // isRunning phải EXPLICITLY false (không phải undefined) mới là Paused
    if (state === "progress" && isRunning === false) return "text-bg-warning";   // Paused
    return {
        progress: "text-bg-info",
        done:     "text-bg-success",
        cancel:   "text-bg-danger",
        blocked:  "text-bg-warning",
        ready:    "text-bg-secondary",
    }[state] || "text-bg-secondary";
}

function getStatusLabel(state, isRunning = true) {
    if (state === "progress" && isRunning === false) return "Paused";  // đã tạm dừng (phải explicit false)
    return {
        progress: "In Progress",
        done:     "Done",
        cancel:   "Cancelled",
        blocked:  "Blocked",
        ready:    "Ready",
    }[state] || state;
}

function getMoStatusClass(state) {
    return {
        draft:     "text-bg-secondary",
        confirmed: "text-bg-info",
        progress:  "text-bg-primary",
        to_close:  "text-bg-warning",
        done:      "text-bg-success",
        cancel:    "text-bg-danger",
    }[state] || "text-bg-secondary";
}

function getMoStatusLabel(state) {
    return {
        draft:     "Draft",
        confirmed: "Confirmed",
        progress:  "In Progress",
        to_close:  "To Close",
        done:      "Done",
        cancel:    "Cancelled",
    }[state] || state;
}

// WO có thể Start khi: ready, hoặc đang "progress" nhưng bị pause (is_running PHẢI === false, không phải undefined)
// Dùng === false để tránh bug khi server cũ chưa trả is_running (undefined != false)
function canStartWo(wo)  { return wo.state === "ready" || (wo.state === "progress" && wo.is_running === false); }
// Pause: progress và KHÔNG phải đang bị pause (is_running=false)
// is_running=undefined (server cũ) → backward-compat: vẫn hiện Pause như trước
function canPauseWo(wo)  { return wo.state === "progress" && wo.is_running !== false; }
function canFinishWo(wo) { return wo.state === "progress"; }

// ─────────────────────────────────────────────────────────────────────────────
// OWL Component — ProductionFlowTab
// ─────────────────────────────────────────────────────────────────────────────

export class ProductionFlowTab extends Component {
    static template = "myco_mrp.ProductionFlowTab";
    static props = { ...standardFieldProps };

    setup() {
        this.orm          = useService("orm");
        this.notification = useService("notification");
        this.action       = useService("action");

        this.state = useState({
            childMos:   [],
            loading:    true,
            expanded:   {},
            // masterInfo: { id, name, state, flow_state, flow_progress, child_count,
            //               managerWo, assemblyWo, packingWo }
            masterInfo: {},
        });

        this.tickState     = useState({ elapsed: 0 });
        this._dataLoadedAt = 0;
        this._tickInterval = null;

        onWillStart(() => this._loadData());
        onWillDestroy(() => this._stopTicker());
    }

    get moId() { return this.props.record.resId; }

    // ── Timer ────────────────────────────────────────────────────────────────

    _startTicker() {
        this._stopTicker();

        // Tick khi có WO đang chạy thực sự:
        //   is_running === true  → đang chạy (server mới trả is_running)
        //   is_running === undefined → server cũ, fallback: tick nếu state=progress (backward-compat)
        const info = this.state.masterInfo;
        const isWoTicking = (wo) => wo && wo.state === "progress" && wo.is_running !== false;
        const needsTimer =
            isWoTicking(info.managerWo)  ||
            isWoTicking(info.assemblyWo) ||
            isWoTicking(info.packingWo)  ||
            this.state.childMos.some(mo => mo.workorders.some(isWoTicking));
        if (!needsTimer) return;

        this._tickInterval = setInterval(() => {
            this.tickState.elapsed = (Date.now() - this._dataLoadedAt) / 60_000;
        }, 1000);
    }

    _stopTicker() {
        if (this._tickInterval) {
            clearInterval(this._tickInterval);
            this._tickInterval = null;
        }
    }

    // ── Tải dữ liệu ─────────────────────────────────────────────────────────

    /**
     * Single RPC: action_get_production_flow_data trả về:
     *   { master: {...}, children: [...], master_workorders: [...] }
     */
    async _loadData({ showLoading = true } = {}) {
        if (showLoading) {
            this.state.loading = true;
        }
        this._stopTicker();
        this.tickState.elapsed = 0;

        try {
            const payload = await this.orm.call(
                "mrp.production",
                "action_get_production_flow_data",
                [[this.moId]],
            );
            this._dataLoadedAt = Date.now();

            // Tìm 3 WO tổng theo flow_role từ master_workorders
            const masterWos  = payload.master_workorders || [];
            const managerWo  = masterWos.find(wo => wo.flow_role === "manager")  || null;
            const assemblyWo = masterWos.find(wo => wo.flow_role === "assembly") || null;
            const packingWo  = masterWos.find(wo => wo.flow_role === "packing")  || null;

            this.state.masterInfo = {
                ...payload.master,
                managerWo,
                assemblyWo,
                packingWo,
            };

            // Giữ trạng thái expanded giữa các lần reload
            const children = payload.children || [];
            for (const mo of children) {
                if (!(mo.id in this.state.expanded)) {
                    this.state.expanded[mo.id] = true;
                }
            }
            this.state.childMos = children;
        } catch (e) {
            this.notification.add(e.message || "Không thể tải dữ liệu Production Flow", { type: "danger" });
        } finally {
            if (showLoading) {
                this.state.loading = false;
            }
            this._startTicker();
        }
    }

    // ── Tính thời gian hiển thị ──────────────────────────────────────────────

    /**
     * duration từ server đã dùng get_duration() (bao gồm open time line lúc load).
     * Client cộng thêm elapsed khi WO đang chạy thực sự.
     * is_running === false  → WO bị Pause → duration tĩnh, không đếm thêm.
     * is_running === undefined → server cũ, fallback: đếm theo state=progress (backward-compat).
     */
    getLiveWoDuration(wo) {
        if (!wo) return 0;
        if (wo.state === "progress" && wo.is_running !== false) {
            return (wo.duration || 0) + this.tickState.elapsed;
        }
        return wo.duration || 0;
    }

    /**
     * Thời gian làm việc Quản lý.
     * hasData  = WO đã start ít nhất 1 lần (state progress hoặc done).
     * isRunning = đang chạy thực sự (is_running !== false).
     */
    get managerParallelTime() {
        const wo = this.state.masterInfo.managerWo;
        if (!wo) {
            return { minutes: 0, isRunning: false, hasData: false };
        }
        const isRunning = wo.state === "progress" && wo.is_running !== false;
        const hasData   = ["progress", "done"].includes(wo.state);
        return {
            minutes:  isRunning ? (wo.duration || 0) + this.tickState.elapsed : (wo.duration || 0),
            isRunning,
            hasData,
        };
    }

    // ── Actions ──────────────────────────────────────────────────────────────

    async _reloadRecord() {
        try { await this.props.record.load(); } catch (_) {}
    }

    async startWo(woId) {
        try {
            await this.orm.call("mrp.workorder", "button_start", [[woId]]);
            this.notification.add("WO đã bắt đầu", { type: "success" });
        } catch (e) {
            this.notification.add(e.message || "Không thể bắt đầu WO", { type: "danger" });
        }
        await this._loadData();
        await this._reloadRecord();
    }

    async pauseWo(woId) {
        try {
            await this.orm.call("mrp.workorder", "button_pending", [[woId]]);
            this.notification.add("WO đã tạm dừng", { type: "warning" });
        } catch (e) {
            this.notification.add(e.message || "Không thể tạm dừng WO", { type: "danger" });
        }
        await this._loadData();
        await this._reloadRecord();
    }

    async finishWo(woId) {
        try {
            await this.orm.call("mrp.workorder", "action_mark_as_done", [[woId]]);
            this.notification.add("WO hoàn thành", { type: "success" });
        } catch (e) {
            this.notification.add(e.message || "Không thể hoàn thành WO", { type: "danger" });
        }
        await this._loadData();
        await this._reloadRecord();
    }

    toggleMo(moId) {
        this.state.expanded[moId] = !this.state.expanded[moId];
    }

    async openMo(moId) {
        await this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "mrp.production",
            res_id: moId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    // ── Helpers expose sang template ─────────────────────────────────────────

    formatDuration(minutes, showSeconds)      { return formatDuration(minutes, showSeconds); }
    // Truyền isRunning để phân biệt "In Progress" vs "Paused" (cùng state=progress)
    getStatusClass(state, isRunning = true)   { return getStatusClass(state, isRunning); }
    getStatusLabel(state, isRunning = true)   { return getStatusLabel(state, isRunning); }
    getMoStatusClass(state)                   { return getMoStatusClass(state); }
    getMoStatusLabel(state)                   { return getMoStatusLabel(state); }
    canStartWo(wo)                            { return canStartWo(wo); }
    canPauseWo(wo)                            { return canPauseWo(wo); }
    canFinishWo(wo)                           { return canFinishWo(wo); }

    get progress() {
        const pct      = Math.max(0, Math.min(100, this.state.masterInfo.flow_progress || 0));
        const flowState = this.state.masterInfo.flow_state;
        let cls = "bg-secondary";
        if (pct >= 100 || flowState === "done")       cls = "bg-success";
        else if (pct > 0 || flowState === "running")  cls = "bg-info";
        return { pct, cls, label: `${pct.toFixed(0)}%` };
    }

    get summary() {
        const mos = this.state.childMos;
        return {
            total:   mos.length,
            running: mos.filter(m => m.state === "progress").length,
            done:    mos.filter(m => ["done", "to_close"].includes(m.state)).length,
        };
    }
}

registry.category("fields").add("production_flow_tab", {
    component: ProductionFlowTab,
});

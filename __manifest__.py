{
    "name": "MyCo MRP Production Flow",
    "version": "19.0.3.0.0",
    "category": "Manufacturing",
    "summary": "Thin MRP orchestration on native Source/Child MO flow.",
    "author": "MyCo",
    "depends": ["mrp"],
    "data": [
        "security/ir.model.access.csv",
        "views/mrp_workorder_views.xml",
        "views/mrp_production_views.xml",
        "views/assembly_start_confirm_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "myco_mrp_production_flow/static/src/css/production_flow.css",
            "myco_mrp_production_flow/static/src/js/production_flow_tab.js",
            "myco_mrp_production_flow/static/src/xml/production_flow_tab.xml",
        ],
    },
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}

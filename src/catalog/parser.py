import json
import zipfile
from pathlib import Path


def _source_type(node_type: str, conn_attrs: dict) -> str:
    cls = conn_attrs.get("class", "")
    if cls == "excel-direct":
        return "excel"
    if cls == "mysql":
        return "mysql"
    if cls == "googledrive":
        return "googledrive"
    if cls == "text-file":
        return "csv"
    if cls == "mdb":
        return "access"
    if "Excel" in node_type:
        return "excel"
    if "Sql" in node_type:
        return "sql"
    return cls or "unknown"


def parse_tfl(file_path: Path) -> dict:
    with zipfile.ZipFile(file_path, "r") as z:
        with z.open("flow") as f:
            data = json.loads(f.read().decode("utf-8", errors="replace"))

    nodes = data.get("nodes", {})
    connections = data.get("connections", {})

    sources = []
    outputs = []
    transforms = []

    for node_id, node in nodes.items():
        base_type = node.get("baseType")
        node_type = node.get("nodeType", "")

        if base_type == "input":
            conn_id = node.get("connectionId")
            conn = connections.get(conn_id, {})
            conn_attrs = conn.get("connectionAttributes", {})
            src_type = _source_type(node_type, conn_attrs)

            raw_table = node.get("connectionAttributes", {}).get("tablename", "") or ""
            source_table = raw_table.strip("[]")

            fields = [
                {
                    "name": f["name"],
                    "type": f.get("type", "unknown"),
                    "ordinal": f.get("ordinal", 0),
                }
                for f in node.get("fields", [])
                if not f.get("isGenerated", False)
            ]

            sources.append({
                "node_id": node_id,
                "node_name": node.get("name"),
                "source_type": src_type,
                "source_table": source_table or None,
                "connection_id": conn_id,
                "server": conn_attrs.get("server"),
                "database_name": conn_attrs.get("dbname"),
                "file_path": conn_attrs.get("filename"),
                "fields": fields,
            })

        elif base_type == "output":
            outputs.append({
                "node_id": node_id,
                "node_name": node.get("name"),
                "destination_table": (node.get("attributes") or {}).get("tablename"),
                "connection_id": node.get("connectionId"),
            })

        elif base_type == "superNode":
            action = node.get("actionNode")
            node_type_super = node.get("nodeType", "")

            # SuperTransform: nodo de limpieza con anotaciones, sin actionNode
            if action is None or "SuperTransform" in node_type_super:
                transforms.append({
                    "node_id": node_id,
                    "node_name": node.get("name"),
                    "transform_type": "clean",
                    "details": {},
                })
                continue

            action_type = action.get("nodeType", "")

            if "Join" in action_type:
                transforms.append({
                    "node_id": node_id,
                    "node_name": node.get("name"),
                    "transform_type": "join",
                    "details": {
                        "join_type": action.get("joinType"),
                        "conditions": action.get("conditions", []),
                    },
                })
            elif "Aggregate" in action_type:
                transforms.append({
                    "node_id": node_id,
                    "node_name": node.get("name"),
                    "transform_type": "aggregate",
                    "details": {
                        "group_by": [f["columnName"] for f in action.get("groupByFields", [])],
                        "aggregates": [
                            {"field": f["columnName"], "function": f["function"]}
                            for f in action.get("aggregateFields", [])
                        ],
                    },
                })
            elif "Pivot" in action_type:
                transforms.append({
                    "node_id": node_id,
                    "node_name": node.get("name"),
                    "transform_type": "pivot",
                    "details": {},
                })
            else:
                transforms.append({
                    "node_id": node_id,
                    "node_name": node.get("name"),
                    "transform_type": action_type.split(".")[-1].lower(),
                    "details": {},
                })

        elif base_type == "container":
            transforms.append({
                "node_id": node_id,
                "node_name": node.get("name"),
                "transform_type": "clean",
                "details": {},
            })

    return {
        "flow_name": file_path.stem,
        "file_path": str(file_path),
        "node_count": len(nodes),
        "sources": sources,
        "outputs": outputs,
        "transforms": transforms,
    }

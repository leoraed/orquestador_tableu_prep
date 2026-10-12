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


def _graph_node_type(base_type: str, node_type: str, action: dict | None) -> str:
    if base_type == "input":
        return "input"
    if base_type == "output":
        return "output"
    if base_type == "container":
        return "clean"
    if base_type == "superNode":
        if action is None or "SuperTransform" in node_type:
            return "clean"
        t = action.get("nodeType", "")
        if "Join" in t:
            return "join"
        if "Aggregate" in t:
            return "aggregate"
        if "Pivot" in t:
            return "pivot"
        if "Union" in t:
            return "union"
    return "clean"


def _extract_annotations(node: dict) -> list:
    ops = []
    for ann_key in ["beforeActionAnnotations", "afterActionAnnotations"]:
        for ann in node.get(ann_key, []):
            an = ann.get("annotationNode", {})
            nt = an.get("nodeType", "")
            if "RenameColumn" in nt:
                ops.append({"op": "Renombrar", "de": an.get("columnName", ""), "a": an.get("rename", "")})
            elif "ChangeColumnType" in nt:
                for col, info in (an.get("fields") or {}).items():
                    ops.append({"op": "Cambiar tipo", "campo": col, "tipo": (info or {}).get("type", "")})
            elif "FilterOperation" in nt:
                ops.append({"op": "Filtrar", "expr": an.get("filterExpression", "")})
            elif "RemoveColumns" in nt:
                for col in an.get("columnNames", []):
                    ops.append({"op": "Eliminar campo", "campo": col})
            elif "AddColumn" in nt:
                ops.append({"op": "Agregar campo", "campo": an.get("columnName", ""), "expr": an.get("expression", "")})
            elif "QuickCalcColumn" in nt:
                ops.append({"op": "Calcular", "campo": an.get("columnName", ""), "expr": an.get("expression", "")})
            elif "Remap" in nt:
                ops.append({"op": "Reasignar valores", "campo": an.get("columnName", ""), "valores": an.get("values", {})})
            elif "DuplicateColumn" in nt:
                ops.append({"op": "Duplicar campo", "campo": an.get("columnName", "")})
    return ops


def _node_details(node: dict, connections: dict) -> dict:
    base_type = node.get("baseType", "")
    node_type = node.get("nodeType", "")

    if base_type == "input":
        conn_id = node.get("connectionId")
        conn = connections.get(conn_id, {})
        conn_attrs = conn.get("connectionAttributes", {})
        return {
            "source_table": (node.get("connectionAttributes", {}).get("tablename") or "").strip("[]"),
            "source_type": _source_type(node_type, conn_attrs),
            "server": conn_attrs.get("server"),
            "database": conn_attrs.get("dbname"),
            "file": conn_attrs.get("filename"),
            "fields": [
                {"name": f["name"], "type": f.get("type", "")}
                for f in node.get("fields", [])
                if not f.get("isGenerated", False)
            ],
        }

    if base_type == "output":
        return {
            "destination_table": (node.get("attributes") or {}).get("tablename"),
        }

    if base_type == "superNode":
        action = node.get("actionNode")
        if action is None or "SuperTransform" in node_type:
            return {"operations": _extract_annotations(node)}
        action_type = action.get("nodeType", "")
        if "Join" in action_type:
            return {
                "join_type": action.get("joinType", ""),
                "conditions": [
                    {
                        "left": c.get("leftExpression", "").strip("[]"),
                        "right": c.get("rightExpression", "").strip("[]"),
                    }
                    for c in action.get("conditions", [])
                ],
            }
        if "Aggregate" in action_type:
            return {
                "group_by": [f["columnName"] for f in action.get("groupByFields", [])],
                "aggregates": [
                    {"field": f["columnName"], "function": f["function"]}
                    for f in action.get("aggregateFields", [])
                ],
            }

    if base_type == "container":
        return {"operations": _extract_annotations(node)}

    return {}


def parse_tfl(file_path: Path) -> dict:
    with zipfile.ZipFile(file_path, "r") as z:
        with z.open("flow") as f:
            data = json.loads(f.read().decode("utf-8", errors="replace"))

    nodes = data.get("nodes", {})
    connections = data.get("connections", {})

    sources = []
    outputs = []
    transforms = []
    graph_nodes = []
    graph_edges = []

    for node_id, node in nodes.items():
        base_type = node.get("baseType", "")
        action = node.get("actionNode")
        g_type = _graph_node_type(base_type, node.get("nodeType", ""), action)
        graph_nodes.append({
            "id": node_id,
            "name": node.get("name", ""),
            "type": g_type,
            "details": _node_details(node, connections),
        })
        for nxt in node.get("nextNodes", []):
            graph_edges.append({"from": node_id, "to": nxt["nextNodeId"]})

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
                {"name": f["name"], "type": f.get("type", "unknown"), "ordinal": f.get("ordinal", 0)}
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
            if action is None or "SuperTransform" in node_type_super:
                transforms.append({"node_id": node_id, "node_name": node.get("name"), "transform_type": "clean", "details": {}})
                continue
            action_type = action.get("nodeType", "")
            if "Join" in action_type:
                transforms.append({
                    "node_id": node_id, "node_name": node.get("name"), "transform_type": "join",
                    "details": {"join_type": action.get("joinType"), "conditions": action.get("conditions", [])},
                })
            elif "Aggregate" in action_type:
                transforms.append({
                    "node_id": node_id, "node_name": node.get("name"), "transform_type": "aggregate",
                    "details": {
                        "group_by": [f["columnName"] for f in action.get("groupByFields", [])],
                        "aggregates": [{"field": f["columnName"], "function": f["function"]} for f in action.get("aggregateFields", [])],
                    },
                })
            elif "Pivot" in action_type:
                transforms.append({"node_id": node_id, "node_name": node.get("name"), "transform_type": "pivot", "details": {}})
            else:
                transforms.append({"node_id": node_id, "node_name": node.get("name"), "transform_type": action_type.split(".")[-1].lower(), "details": {}})

        elif base_type == "container":
            transforms.append({"node_id": node_id, "node_name": node.get("name"), "transform_type": "clean", "details": {}})

    return {
        "flow_name": file_path.stem,
        "file_path": str(file_path),
        "node_count": len(nodes),
        "sources": sources,
        "outputs": outputs,
        "transforms": transforms,
        "graph": {"nodes": graph_nodes, "edges": graph_edges},
    }

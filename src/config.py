import yaml
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
_FLOWS_YAML = BASE_DIR / "config" / "flows.yaml"


def _load_yaml() -> dict:
    with open(_FLOWS_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_yaml(data: dict) -> None:
    with open(_FLOWS_YAML, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def load_settings() -> dict:
    with open(BASE_DIR / "config" / "settings.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_flows() -> list[dict]:
    flows = _load_yaml().get("flows", []) or []
    for flow in flows:
        flow.pop("schedules", None)
        flow.pop("schedule", None)
        flow.pop("depends_on", None)
        flow.setdefault("reintentos", 0)
        flow.setdefault("reintento_espera_min", 5)
        flow.setdefault("enabled", True)
    return flows


def load_pipelines() -> list[dict]:
    pipelines = _load_yaml().get("pipelines", []) or []
    for pipeline in pipelines:
        if "schedules" not in pipeline:
            pipeline["schedules"] = []
        elif isinstance(pipeline["schedules"], str):
            s = pipeline["schedules"]
            pipeline["schedules"] = [s] if s else []
        pipeline["schedules"] = [s for s in (pipeline["schedules"] or []) if s]
        for step in pipeline.get("flows", []):
            step.setdefault("depends_on", [])
    return pipelines


def load_carpetas() -> list[str]:
    return _load_yaml().get("carpetas", []) or []


def descubrir_tfl(carpetas: list[str]) -> list[dict]:
    resultado = []
    for carpeta in carpetas:
        p = Path(carpeta)
        if not p.exists() or not p.is_dir():
            continue
        for ext in ("*.tfl", "*.tflx"):
            for tfl in sorted(p.glob(ext)):
                stat = tfl.stat()
                resultado.append({
                    "nombre": tfl.stem,
                    "path": str(tfl),
                    "carpeta": str(p),
                    "extension": tfl.suffix,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "modificado": datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M"),
                })
    return resultado


settings = load_settings()

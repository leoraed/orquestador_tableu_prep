"""
Permisos, roles y qué permiso exige cada ruta de la app.

Un usuario puede tener varios roles; sus permisos son la unión. Las rutas que no
figuran en REGLAS quedan denegadas (tests/test_login.py verifica que no falte ninguna).
"""
PERMISOS: dict[str, str] = {
    "tableros.ver": "Ver tableros",
    "tableros.publicar": "Publicar tableros y versiones nuevas de los propios",
    "tableros.administrar": "Editar o borrar tableros de otros",
    "catalogo.ver": "Ver el catálogo de datos",
    "catalogo.documentar": "Editar descripciones y tags del catálogo",
    "catalogo.parsear": "Reprocesar los flows del catálogo",
    "modelos.ver": "Ver modelos semánticos",
    "modelos.consultar": "Consultar el DW a través de los modelos (MCP)",
    "modelos.probar": "Ejecutar expresiones contra el DW desde el editor (Probar, Validar)",
    "modelos.editar": "Editar campos, entidades y joins; crear backups",
    "modelos.administrar": "Importar, restaurar y eliminar modelos",
    "etl.ver": "Ver flows, pipelines, tasks, historial, grafo y logs",
    "etl.ejecutar": "Ejecutar y cancelar flows, pipelines y tasks",
    "etl.configurar": "Crear, editar y borrar flows, pipelines, tasks y carpetas",
    "sistema.configuracion": "Configuración general (incluye tokens de Telegram y Tableau)",
    "sistema.usuarios": "Administrar usuarios y roles",
    "sistema.auditoria": "Ver la auditoría",
}

_LECTOR = {"tableros.ver", "catalogo.ver", "modelos.ver"}
_ANALISTA = _LECTOR | {"modelos.consultar", "tableros.publicar", "catalogo.documentar"}

ROLES: dict[str, dict] = {
    "lector": {"descripcion": "Ve tableros, catálogo y modelos.", "permisos": _LECTOR},
    "analista": {"descripcion": "Lector + consulta el DW con Claude, publica tableros y documenta el catálogo.",
                 "permisos": _ANALISTA},
    "modelador": {"descripcion": "Analista + edita y administra modelos semánticos y reprocesa el catálogo.",
                  "permisos": _ANALISTA | {"modelos.probar", "modelos.editar", "modelos.administrar",
                                           "catalogo.parsear", "etl.ver"}},
    "operador": {"descripcion": "Lector + ve y ejecuta flows, pipelines y tasks.",
                 "permisos": _LECTOR | {"etl.ver", "etl.ejecutar"}},
    "admin": {"descripcion": "Todo, incluida la configuración, los usuarios y la auditoría.",
              "permisos": set(PERMISOS)},
}


def permisos_de(roles) -> frozenset[str]:
    p: set[str] = set()
    for r in roles:
        p |= ROLES.get(r, {}).get("permisos", set())
    return frozenset(p)


def normalizar_roles(texto_o_lista) -> list[str]:
    items = texto_o_lista.split(",") if isinstance(texto_o_lista, str) else (texto_o_lista or [])
    roles = []
    for r in items:
        r = r.strip().lower()
        if r and r not in roles:
            if r not in ROLES:
                raise ValueError(f"Rol inválido: {r}. Opciones: {', '.join(ROLES)}.")
            roles.append(r)
    return roles


# Valores especiales: PUBLICO no pide login; SESION pide login pero ningún permiso.
PUBLICO = "publico"
SESION = "sesion"

_G, _P, _U, _D, _PA = "GET", "POST", "PUT", "DELETE", "PATCH"

REGLAS: dict[tuple[str, str], str] = {
    # Acceso
    (_G, "/login"): PUBLICO, (_P, "/login"): PUBLICO, (_G, "/health"): PUBLICO,
    (_P, "/logout"): SESION, (_G, "/perfil"): SESION,
    (_P, "/api/perfil/password"): SESION, (_P, "/api/perfil/token-mcp"): SESION,
    (_G, "/"): SESION,  # la portada redirige a Tableros si no puede ver el ETL

    # Usuarios y auditoría
    (_G, "/usuarios"): "sistema.usuarios", (_G, "/api/usuarios"): "sistema.usuarios",
    (_P, "/api/usuarios"): "sistema.usuarios", (_U, "/api/usuarios/{uid}"): "sistema.usuarios",
    (_P, "/api/usuarios/{uid}/reset-password"): "sistema.usuarios",
    (_P, "/api/usuarios/{uid}/revocar-token"): "sistema.usuarios",
    (_G, "/auditoria"): "sistema.auditoria", (_G, "/api/auditoria"): "sistema.auditoria",

    # ETL
    (_G, "/flows"): "etl.ver", (_G, "/pipelines"): "etl.ver", (_G, "/tasks"): "etl.ver",
    (_G, "/historial"): "etl.ver", (_G, "/grafo"): "etl.ver", (_G, "/logs"): "etl.ver",
    (_G, "/api/grafo/estados"): "etl.ver", (_G, "/api/ejecuciones"): "etl.ver",
    (_G, "/api/ejecuciones/{eid}"): "etl.ver",
    (_P, "/api/flows/{nombre}/ejecutar"): "etl.ejecutar",
    (_P, "/api/pipelines/{nombre}/ejecutar"): "etl.ejecutar",
    (_P, "/api/pipelines/{pipeline_name}/steps/{step_name}/ejecutar"): "etl.ejecutar",
    (_P, "/api/tasks/{nombre}/ejecutar"): "etl.ejecutar",
    (_P, "/api/ejecuciones/{eid}/cancelar"): "etl.ejecutar",
    (_P, "/api/flows"): "etl.configurar", (_P, "/api/flows/{nombre}/editar"): "etl.configurar",
    (_P, "/api/flows/{nombre}/eliminar"): "etl.configurar",
    (_P, "/api/pipelines"): "etl.configurar", (_P, "/api/pipelines/{nombre}/editar"): "etl.configurar",
    (_P, "/api/pipelines/{nombre}/eliminar"): "etl.configurar",
    (_P, "/api/pipelines/{pipeline_name}/flows/{flow_name}/deps"): "etl.configurar",
    (_P, "/api/tasks"): "etl.configurar", (_P, "/api/tasks/{nombre}/editar"): "etl.configurar",
    (_P, "/api/tasks/{nombre}/eliminar"): "etl.configurar",
    (_P, "/api/carpetas"): "etl.configurar", (_P, "/api/carpetas/eliminar"): "etl.configurar",

    # Configuración
    (_G, "/configuracion"): "sistema.configuracion", (_P, "/api/configuracion"): "sistema.configuracion",

    # Catálogo
    (_G, "/catalogo"): "catalogo.ver", (_G, "/catalogo/{flow_name}"): "catalogo.ver",
    (_G, "/api/catalog/flows"): "catalogo.ver", (_G, "/api/catalog/flows/{flow_name}"): "catalogo.ver",
    (_G, "/api/catalog/fields"): "catalogo.ver", (_G, "/api/catalog/buscar-campos"): "catalogo.ver",
    (_PA, "/api/catalog/fields/{field_id}"): "catalogo.documentar",
    (_P, "/api/catalog/parse"): "catalogo.parsear",

    # Modelos semánticos
    (_G, "/modelos"): "modelos.ver", (_G, "/modelos/{nombre}"): "modelos.ver",
    (_G, "/api/modelos/{nombre}/campos"): "modelos.ver", (_G, "/api/modelos/{nombre}/columnas"): "modelos.ver",
    (_G, "/api/modelos/{nombre}/yaml"): "modelos.ver", (_G, "/api/modelos/{nombre}/backups"): "modelos.ver",
    (_G, "/api/modelos/backups/{backup_id}/yaml"): "modelos.ver", (_G, "/api/modelos-eliminados"): "modelos.ver",
    (_P, "/api/modelos/{nombre}/probar"): "modelos.probar", (_P, "/api/modelos/{nombre}/validar"): "modelos.probar",
    (_P, "/api/modelos/{nombre}/convertir"): "modelos.editar",
    (_P, "/api/modelos"): "modelos.editar", (_U, "/api/modelos/{nombre}"): "modelos.editar",
    (_P, "/api/modelos/{nombre}/editar"): "modelos.editar",
    (_P, "/api/modelos/{nombre}/campos"): "modelos.editar",
    (_U, "/api/modelos/{nombre}/campos/{kind}/{campo_id}"): "modelos.editar",
    (_D, "/api/modelos/{nombre}/campos/{kind}/{campo_id}"): "modelos.editar",
    (_P, "/api/modelos/{nombre}/entidades"): "modelos.editar",
    (_U, "/api/modelos/{nombre}/entidades/{entity_id}"): "modelos.editar",
    (_D, "/api/modelos/{nombre}/entidades/{entity_id}"): "modelos.editar",
    (_P, "/api/modelos/{nombre}/backups"): "modelos.editar",
    (_P, "/api/modelos/importar-twb"): "modelos.administrar", (_P, "/api/modelos/importar-yaml"): "modelos.administrar",
    (_P, "/api/modelos/{nombre}/eliminar"): "modelos.administrar",
    (_P, "/api/modelos/backups/{backup_id}/restaurar"): "modelos.administrar",

    # Tableros (versiones/edición/borrado además verifican autoría en la ruta)
    (_G, "/tableros"): "tableros.ver", (_G, "/tableros/{slug}"): "tableros.ver",
    (_G, "/tableros/{slug}/contenido"): "tableros.ver", (_G, "/api/tableros"): "tableros.ver",
    (_P, "/api/tableros"): "tableros.publicar", (_P, "/api/tableros/{slug}/versiones"): "tableros.publicar",
    (_U, "/api/tableros/{slug}"): "tableros.publicar", (_D, "/api/tableros/{slug}"): "tableros.publicar",
}

# Permiso que exige cada herramienta del MCP
HERRAMIENTAS_MCP: dict[str, str] = {
    "listar_modelos": "modelos.ver", "get_modelo": "modelos.ver", "buscar_campos": "modelos.ver",
    "get_yaml": "modelos.ver", "consultar": "modelos.consultar",
    "publicar_dashboard": "tableros.publicar", "listar_dashboards": "tableros.ver",
    "obtener_dashboard": "tableros.ver",
}

"""Helpers compartidos por los tests de la app web."""
from src import acceso_web, usuarios

PASSWORD = "clave-segura-123"


def iniciar_sesion(client, Session, monkeypatch, roles=("admin",), usuario="admin.test", nombre="Admin Test"):
    """Crea el usuario en el catálogo de prueba e inicia sesión con el TestClient."""
    monkeypatch.setattr(acceso_web, "SessionCatalog", Session)   # la auditoría de la app escribe ahí
    db = Session()
    if not usuarios.buscar(db, usuario):
        u, _ = usuarios.crear(db, usuario, nombre, list(roles), password=PASSWORD)
        u.debe_cambiar_password = False
        db.commit()
    db.close()
    client.headers.update({"origin": "http://testserver"})       # CSRF: mismo origen
    client.cookies.clear()
    r = client.post("/login", data={"usuario": usuario, "password": PASSWORD, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return client

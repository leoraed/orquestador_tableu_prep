"""
Traduce fórmulas de campos calculados de Tableau a SQL MySQL.

La traducción es determinística (sin LLM):
- Sintaxis: COUNTD → COUNT(DISTINCT), IF/ELSEIF → CASE WHEN, IIF, ZN, ISNULL,
  funciones de fecha, comentarios //, strings con comillas dobles, #fechas#.
- Referencias: cada [campo] se resuelve contra el workbook. Si es un cálculo,
  se reemplaza por su fórmula traducida (recursivamente); si es una columna
  física, por su columna en la tabla.

Lo que no tiene equivalente SQL directo (LOD, cálculos de tabla, parámetros,
blending) levanta NoTraducible con el motivo, para revisión manual.
"""
import re
from typing import Callable


class NoTraducible(Exception):
    """La fórmula usa algo que no se puede convertir a SQL de forma segura."""


_TOKEN_RE = re.compile(
    r"""
     (?P<comment>//[^\n]*)
    |(?P<ws>\s+)
    |(?P<str>'(?:[^'\\]|\\.|'')*'|"(?:[^"\\]|\\.|"")*")
    |(?P<date>\#[^#]+\#)
    |(?P<ref>\[(?:[^\]]|\]\])*\](?:\.\[(?:[^\]]|\]\])*\])?)
    |(?P<num>\d+(?:\.\d+)?)
    |(?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    |(?P<op><=|>=|<>|!=|==|[-+*/%=<>(),{}:^])
    |(?P<other>.)
    """,
    re.VERBOSE,
)

# Funciones con el mismo nombre y semántica en MySQL
_PASSTHROUGH = {
    "SUM", "AVG", "MIN", "MAX", "COUNT", "ABS", "ROUND", "FLOOR", "CEILING",
    "SQRT", "POWER", "EXP", "LN", "LOG", "SIGN", "YEAR", "QUARTER", "MONTH",
    "DAY", "UPPER", "LOWER", "LEFT", "RIGHT", "MID", "TRIM", "LTRIM", "RTRIM",
    "DATE", "IFNULL", "COALESCE", "NOW", "REPLACE",
}

# Cálculos de tabla y agregaciones que dependen de la vista de Tableau
_TABLE_CALCS = {
    "ATTR", "TOTAL", "LOOKUP", "INDEX", "FIRST", "LAST", "SIZE",
    "PREVIOUS_VALUE", "RANK", "RANK_DENSE", "RANK_MODIFIED", "RANK_PERCENTILE",
    "RANK_UNIQUE",
}
_TABLE_CALC_PREFIXES = ("WINDOW_", "RUNNING_")

_KEYWORDS = {
    "THEN", "ELSE", "END", "AND", "OR", "NOT", "CASE", "WHEN", "IN",
    "TRUE", "FALSE", "NULL", "IS",
}

_DATEPART_FUNCS = {
    "year": "YEAR", "quarter": "QUARTER", "month": "MONTH", "day": "DAY",
    "weekday": "DAYOFWEEK",  # Tableau y MySQL: domingo = 1
    "hour": "HOUR", "minute": "MINUTE", "second": "SECOND",
}
_INTERVAL_UNITS = {
    "year": "YEAR", "quarter": "QUARTER", "month": "MONTH", "week": "WEEK",
    "day": "DAY", "hour": "HOUR", "minute": "MINUTE", "second": "SECOND",
}


def _tokenize(formula: str) -> list[tuple[str, str]]:
    return [
        (m.lastgroup, m.group())
        for m in _TOKEN_RE.finditer(formula)
        if m.lastgroup != "comment"
    ]


def _sql_string(literal: str) -> str:
    """Normaliza un literal de Tableau ('..' o "..") a string SQL con comilla simple."""
    quote, body = literal[0], literal[1:-1]
    body = body.replace(quote * 2, quote)
    return "'" + body.replace("'", "''") + "'"


def _split_ref(ref: str) -> list[str]:
    """'[a].[b]' → ['a', 'b'];  '[a]' → ['a']  (desescapa ]])."""
    parts = re.findall(r"\[((?:[^\]]|\]\])*)\]", ref)
    return [p.replace("]]", "]") for p in parts]


class _Translator:
    def __init__(self, tokens: list[tuple[str, str]], resolve_ref: Callable[[str], str]):
        self.toks = tokens
        self.resolve_ref = resolve_ref

    # ── helpers ──────────────────────────────────────────────────────────────
    def _next_sig(self, i: int) -> int:
        while i < len(self.toks) and self.toks[i][0] == "ws":
            i += 1
        return i

    def _prev_sig(self, i: int) -> int:
        i -= 1
        while i >= 0 and self.toks[i][0] == "ws":
            i -= 1
        return i

    def _match_paren(self, open_idx: int) -> int:
        depth = 0
        for j in range(open_idx, len(self.toks)):
            v = self.toks[j][1]
            if v == "(":
                depth += 1
            elif v == ")":
                depth -= 1
                if depth == 0:
                    return j
        raise NoTraducible("paréntesis sin cerrar")

    def _split_args(self, start: int, end: int) -> list[list[tuple[str, str]]]:
        args, cur, depth = [], [], 0
        for kind, val in self.toks[start:end]:
            if val == "(":
                depth += 1
            elif val == ")":
                depth -= 1
            if val == "," and depth == 0:
                args.append(cur)
                cur = []
            else:
                cur.append((kind, val))
        if cur or args:
            args.append(cur)
        return args

    @staticmethod
    def _literal_arg(arg: list[tuple[str, str]], func: str) -> str:
        sig = [t for t in arg if t[0] != "ws"]
        if len(sig) != 1 or sig[0][0] != "str":
            raise NoTraducible(f"{func} con parte de fecha no literal")
        return sig[0][1][1:-1].lower()

    # ── traducción ───────────────────────────────────────────────────────────
    def translate(self) -> str:
        out: list[str] = []
        i = 0
        while i < len(self.toks):
            kind, val = self.toks[i]

            if kind == "ws":
                out.append(" ")
            elif kind == "str":
                out.append(_sql_string(val))
            elif kind == "date":
                out.append("'" + val[1:-1].strip() + "'")
            elif kind == "num":
                out.append(val)
            elif kind == "ref":
                out.append(self.resolve_ref(val))
            elif kind == "op":
                if val in "{}:":
                    raise NoTraducible("expresión LOD ({ FIXED / INCLUDE / EXCLUDE })")
                if val == "^":
                    raise NoTraducible("operador ^")
                if val == "+":
                    neighbors = (self._prev_sig(i), self._next_sig(i + 1))
                    if any(0 <= n < len(self.toks) and self.toks[n][0] == "str" for n in neighbors):
                        raise NoTraducible("concatenación de strings con +")
                out.append("=" if val == "==" else val)
            elif kind == "ident":
                up = val.upper()
                nxt = self._next_sig(i + 1)
                is_call = nxt < len(self.toks) and self.toks[nxt][1] == "("
                if is_call and up not in _KEYWORDS and up not in ("IF", "ELSEIF"):
                    close = self._match_paren(nxt)
                    out.append(self._function(up, nxt + 1, close))
                    i = close + 1
                    continue
                if up == "IF":
                    out.append("CASE WHEN")
                elif up == "ELSEIF":
                    out.append("WHEN")
                elif up in _KEYWORDS:
                    out.append(up)
                else:
                    raise NoTraducible(f"identificador desconocido: {val}")
            else:
                raise NoTraducible(f"carácter no soportado: {val!r}")
            i += 1

        return re.sub(r"\s+", " ", "".join(out)).strip()

    def _function(self, name: str, start: int, end: int) -> str:
        raw_args = self._split_args(start, end)
        args = [_Translator(a, self.resolve_ref).translate() for a in raw_args]

        if name in _TABLE_CALCS or name.startswith(_TABLE_CALC_PREFIXES):
            raise NoTraducible(f"cálculo de tabla de Tableau ({name})")
        if name in _PASSTHROUGH:
            return f"{name}({', '.join(args)})"
        if name == "COUNTD":
            return f"COUNT(DISTINCT {args[0]})"
        if name == "ZN":
            return f"COALESCE({args[0]}, 0)"
        if name == "ISNULL":
            return f"({args[0]} IS NULL)"
        if name == "IIF":
            if len(args) < 2:
                raise NoTraducible("IIF con menos de 2 argumentos")
            sql = f"CASE WHEN {args[0]} THEN {args[1]}"
            if len(args) >= 3:
                sql += f" ELSE {args[2]}"
            return sql + " END"
        if name == "TODAY":
            return "CURDATE()"
        if name == "LEN":
            return f"CHAR_LENGTH({args[0]})"
        if name == "CONTAINS":
            return f"(LOCATE({args[1]}, {args[0]}) > 0)"
        if name == "STR":
            return f"CAST({args[0]} AS CHAR)"
        if name == "INT":
            return f"TRUNCATE({args[0]}, 0)"
        if name == "DATEPART":
            part = self._literal_arg(raw_args[0], name)
            if part not in _DATEPART_FUNCS:
                raise NoTraducible(f"DATEPART('{part}') no soportado")
            return f"{_DATEPART_FUNCS[part]}({args[1]})"
        if name == "DATETRUNC":
            part = self._literal_arg(raw_args[0], name)
            x = args[1]
            if part == "day":
                return f"DATE({x})"
            if part == "month":
                return f"DATE_SUB(DATE({x}), INTERVAL DAYOFMONTH({x}) - 1 DAY)"
            if part == "year":
                return f"MAKEDATE(YEAR({x}), 1)"
            raise NoTraducible(f"DATETRUNC('{part}') no soportado")
        if name == "DATEADD":
            part = self._literal_arg(raw_args[0], name)
            if part not in _INTERVAL_UNITS:
                raise NoTraducible(f"DATEADD('{part}') no soportado")
            return f"DATE_ADD({args[2]}, INTERVAL {args[1]} {_INTERVAL_UNITS[part]})"
        if name == "DATEDIFF":
            part = self._literal_arg(raw_args[0], name)
            if part != "day":
                # Tableau cuenta límites cruzados; TIMESTAMPDIFF cuenta unidades completas
                raise NoTraducible(f"DATEDIFF('{part}') no tiene equivalente exacto")
            return f"DATEDIFF(DATE({args[2]}), DATE({args[1]}))"

        raise NoTraducible(f"función no soportada: {name}")


def translate_formula(formula: str, resolve_ref: Callable[[str], str]) -> str:
    """
    Traduce una fórmula de Tableau a SQL MySQL.
    resolve_ref recibe cada referencia tal cual aparece ('[campo]' o '[a].[b]')
    y devuelve su SQL. Levanta NoTraducible si algo no se puede convertir.
    """
    if "__tableau_internal_object_id__" in formula:
        raise NoTraducible("objeto interno de Tableau")
    return _Translator(_tokenize(formula), resolve_ref).translate()


def make_resolver(
    defs: dict[str, dict],
    columns: dict[str, tuple[str, str]],
    base_table: str,
    ds_names: set[str],
    current_ds: str,
) -> Callable[[str], str]:
    """
    Arma un resolvedor de referencias para un datasource.

    defs:       {'[campo]': {'formula': ...}} — todas las columnas del datasource.
    columns:    {'[campo]': (tabla, columna_remota)} — del <cols><map> del TWB;
                tabla None si la fuente es publicada (sqlproxy).
    base_table: tabla base; sus columnas se emiten sin prefijo de tabla.
    ds_names:   nombres internos de todos los datasources (para detectar blending).
    """
    cache: dict[str, str] = {}
    in_progress: set[str] = set()

    def physical(key: str) -> str:
        if key in columns:
            table, remote = columns[key]
            if table is None:
                # Fuente publicada (sqlproxy): no hay tabla real; Tableau renombra las
                # columnas repetidas como "col (tabla)" → `tabla`.`col`
                m = _COL_DE_TABLA_RE.match(remote)
                return f"`{m.group(2)}`.`{m.group(1)}`" if m else f"`{remote}`"
            return f"`{remote}`" if table == base_table else f"`{table}`.`{remote}`"
        return f"`{_split_ref(key)[0]}`"

    def resolve(ref: str) -> str:
        parts = _split_ref(ref)
        if parts[0] == "Parameters":
            raise NoTraducible(f"parámetro de Tableau ({ref})")
        if len(parts) == 2:
            if parts[0] in ds_names:
                if parts[0] != current_ds:
                    raise NoTraducible(f"referencia a otra fuente de datos ({ref})")
                parts = parts[1:]
            else:
                return f"`{parts[0]}`.`{parts[1]}`"

        key = f"[{parts[0]}]"
        info = defs.get(key)
        if not info or not info.get("formula"):
            return physical(key)

        if key in cache:
            return cache[key]
        if key in in_progress:
            raise NoTraducible(f"referencia circular en {key}")
        in_progress.add(key)
        try:
            sql = "(" + translate_formula(info["formula"], resolve) + ")"
        finally:
            in_progress.discard(key)
        cache[key] = sql
        return sql

    return resolve


# Nombres internos de Tableau: [Calculation_123…] o copias "Ventas (copia)_2231533646353948679"
_CALC_INTERNO_RE = re.compile(r"^Calculation_\d+$|_\d{10,}$")
# Columna duplicada renombrada por Tableau: "contacto_id (stagedata_comercial_funnel_ventas)"
_COL_DE_TABLA_RE = re.compile(r"^(\w+) \((\w+)\)$")
# `col`, col, `tabla`.`col` — no necesitan paréntesis al insertarse
_COLUMNA_SIMPLE_RE = re.compile(r"^(`[^`]+`|\w+)(\.(`[^`]+`|\w+))?$")


def make_field_resolver(
    campos: dict[str, dict],
    es_tableau: Callable[[str], bool],
) -> Callable[[str], str]:
    """
    Resolvedor para convertir desde el editor, donde no está el workbook.
    Las referencias [X] se buscan por nombre entre los campos del modelo:

    campos:     {nombre: {"expression": ..., "tableau_formula": ...}}
    es_tableau: indica si una expresión todavía está en sintaxis Tableau.

    - Campo con SQL válido → se inserta su expresión.
    - Campo todavía en Tableau → se convierte recursivamente.
    - Nombre interno de Tableau que no está en el modelo → NoTraducible.
    - Cualquier otro nombre → columna física.
    """
    cache: dict[str, str] = {}
    in_progress: set[str] = set()

    def resolve(ref: str) -> str:
        parts = _split_ref(ref)
        if parts[0] == "Parameters":
            raise NoTraducible(f"parámetro de Tableau ({ref})")
        if len(parts) == 2:
            return f"`{parts[0]}`.`{parts[1]}`"

        nombre = parts[0]
        campo = campos.get(nombre)
        if campo is None:
            if _CALC_INTERNO_RE.search(nombre):
                raise NoTraducible(f"referencia a un cálculo que no está en el modelo ({ref})")
            m = _COL_DE_TABLA_RE.match(nombre)
            if m:
                return f"`{m.group(2)}`.`{m.group(1)}`"
            return f"`{nombre}`"

        if nombre in cache:
            return cache[nombre]
        if nombre in in_progress:
            raise NoTraducible(f"referencia circular en {ref}")
        in_progress.add(nombre)
        try:
            expr = (campo.get("expression") or "").strip()
            if expr and not es_tableau(expr):
                sql = expr if _COLUMNA_SIMPLE_RE.match(expr) else f"({expr})"
            else:
                formula = campo.get("tableau_formula") or expr
                sql = "(" + translate_formula(formula, resolve) + ")"
        finally:
            in_progress.discard(nombre)
        cache[nombre] = sql
        return sql

    return resolve

"""
Cresce Farma 360 — Backend API
Stack: Python · Flask · SQLite · JWT
Níveis de acesso: consultor | gestor | franqueadora
"""

import sqlite3, jwt, os, json
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, g
from flask_cors import CORS

app = Flask(__name__)


CORS(app, origins="*", allow_headers=["Content-Type","Authorization"], methods=["GET","POST","PUT","DELETE","OPTIONS"])

SECRET_KEY = os.environ.get("SECRET_KEY", "cf360-secret-2025-troque-em-producao")
# Railway: configure um Volume em /data para persistência
# Sem volume, o banco reseta a cada deploy — OK para testes, ruim para produção
_data_dir = "/data" if os.path.isdir("/data") else "."
DB_PATH = os.environ.get("DB_PATH", os.path.join(_data_dir, "crescefarma.db"))
TOKEN_EXP  = int(os.environ.get("TOKEN_EXP_HOURS", 12))

# ══════════════════════════════════════════
# BANCO DE DADOS
# ══════════════════════════════════════════

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db: db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript("""
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS usuarios (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        nome        TEXT NOT NULL,
        email       TEXT UNIQUE NOT NULL,
        senha_hash  TEXT NOT NULL,
        perfil      TEXT NOT NULL CHECK(perfil IN ('consultor','gestor','franqueadora')),
        celula      TEXT,
        ativo       INTEGER DEFAULT 1,
        criado_em   TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS lojas (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        nome          TEXT NOT NULL,
        celula        TEXT NOT NULL,
        cidade        TEXT,
        gerente       TEXT,
        telefone      TEXT,
        status        TEXT DEFAULT 'ok' CHECK(status IN ('ok','atencao','pendente')),
        fase          INTEGER DEFAULT 1 CHECK(fase IN (1,2,3)),
        consultor_id  INTEGER REFERENCES usuarios(id),
        criado_em     TEXT DEFAULT (datetime('now')),
        atualizado_em TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS visitas (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        loja_id        INTEGER NOT NULL REFERENCES lojas(id),
        consultor_id   INTEGER NOT NULL REFERENCES usuarios(id),
        data_visita    TEXT NOT NULL,
        fase           INTEGER NOT NULL CHECK(fase IN (1,2,3)),
        objetivo       TEXT,
        obs_campo      TEXT,
        dor_gerente    TEXT,
        dados_fin      TEXT,
        checks_json    TEXT DEFAULT '{}',
        lev_json       TEXT DEFAULT '{}',
        ferr_json      TEXT DEFAULT '{}',
        ia_output      TEXT,
        yungas_texto   TEXT,
        enviado_yungas INTEGER DEFAULT 0,
        status         TEXT DEFAULT 'em_andamento' CHECK(status IN ('em_andamento','concluida')),
        criado_em      TEXT DEFAULT (datetime('now')),
        atualizado_em  TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS acordos (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        visita_id     INTEGER REFERENCES visitas(id),
        loja_id       INTEGER NOT NULL REFERENCES lojas(id),
        consultor_id  INTEGER NOT NULL REFERENCES usuarios(id),
        descricao     TEXT NOT NULL,
        responsavel   TEXT,
        prazo         TEXT,
        fase          INTEGER DEFAULT 1,
        status        TEXT DEFAULT 'aberto' CHECK(status IN ('aberto','concluido','cancelado')),
        criado_em     TEXT DEFAULT (datetime('now')),
        atualizado_em TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS eventos (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        loja_id       INTEGER REFERENCES lojas(id),
        consultor_id  INTEGER NOT NULL REFERENCES usuarios(id),
        data_evento   TEXT NOT NULL,
        descricao     TEXT NOT NULL,
        tipo          TEXT DEFAULT 'lembrete',
        concluido     INTEGER DEFAULT 0,
        criado_em     TEXT DEFAULT (datetime('now'))
    );
    """)
    db.commit()

    # Seed: usuários iniciais se não existirem
    seeds = [
        ("Gestor Cresce Farma 360", "gestor@crescefarma.com",   "gestor123",       "gestor",       "Todas"),
        ("Consultor Demo",     "consultor@focofarma.com","consultor123",    "consultor",    "Amarela"),
        ("Franqueadora",       "franquia@focofarma.com", "franquia123",     "franqueadora", None),
    ]
    for nome, email, senha, perfil, celula in seeds:
        existe = db.execute("SELECT id FROM usuarios WHERE email=?", (email,)).fetchone()
        if not existe:
            h = generate_password_hash(senha)
            db.execute(
                "INSERT INTO usuarios (nome,email,senha_hash,perfil,celula) VALUES (?,?,?,?,?)",
                (nome, email, h, perfil, celula)
            )

    # Seed: lojas iniciais
    lojas_seed = [
        ("Farmácia Bom Jesus Centro","amarela","Curitiba","Ana Paula","(41) 99999-0001","ok",1),
        ("Farmácia Bom Jesus Norte","amarela","Curitiba","Marcos Lima","(41) 99999-0002","atencao",2),
        ("Farmácia Central Norte","verde","Curitiba","Patrícia Souza","(41) 99999-0003","pendente",2),
        ("Farmácia Park Sul","turquesa","Curitiba","Roberto Alves","(41) 99999-0004","ok",3),
        ("Farmácia Recanto das Flores","turquesa","São José dos Pinhais","Fernanda Costa","(41) 99999-0005","atencao",3),
        ("Farmácia Nova Esperança","coral","Pinhais","Carlos Moura","(41) 99999-0006","atencao",1),
        ("Farmácia Boa Saúde","laranja","Araucária","Juliana Ramos","(41) 99999-0007","ok",2),
        ("Farmácia Vida Plena","cafe","Colombo","Eduardo Neri","(41) 99999-0008","ok",3),
    ]
    consultor_id = db.execute("SELECT id FROM usuarios WHERE perfil='consultor' LIMIT 1").fetchone()
    if consultor_id and not db.execute("SELECT id FROM lojas LIMIT 1").fetchone():
        for l in lojas_seed:
            db.execute(
                "INSERT INTO lojas (nome,celula,cidade,gerente,telefone,status,fase,consultor_id) VALUES (?,?,?,?,?,?,?,?)",
                (*l, consultor_id["id"])
            )
    db.commit()
    db.close()


# ══════════════════════════════════════════
# AUTH — JWT
# ══════════════════════════════════════════

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization","").replace("Bearer ","")
        if not token:
            return jsonify({"erro": "Token ausente"}), 401
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            g.usuario = payload
        except jwt.ExpiredSignatureError:
            return jsonify({"erro": "Token expirado"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"erro": "Token inválido"}), 401
        return f(*args, **kwargs)
    return decorated

def perfil_required(*perfis):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if g.usuario.get("perfil") not in perfis:
                return jsonify({"erro": "Acesso não autorizado para este perfil"}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator

def row_to_dict(row):
    return dict(row) if row else None

def rows_to_list(rows):
    return [dict(r) for r in rows]


# ══════════════════════════════════════════
# ROTAS — AUTH
# ══════════════════════════════════════════

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    email = data.get("email","").strip().lower()
    senha = data.get("senha","")
    if not email or not senha:
        return jsonify({"erro": "E-mail e senha obrigatórios"}), 400
    db = get_db()
    user = db.execute("SELECT * FROM usuarios WHERE email=? AND ativo=1", (email,)).fetchone()
    if not user or not check_password_hash(user["senha_hash"], senha):
        return jsonify({"erro": "Credenciais inválidas"}), 401
    exp = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXP)
    token = jwt.encode({
        "id": user["id"], "nome": user["nome"],
        "email": user["email"], "perfil": user["perfil"],
        "celula": user["celula"], "exp": exp
    }, SECRET_KEY, algorithm="HS256")
    return jsonify({
        "token": token,
        "usuario": {
            "id": user["id"], "nome": user["nome"],
            "email": user["email"], "perfil": user["perfil"],
            "celula": user["celula"]
        }
    })

@app.route("/api/auth/me", methods=["GET"])
@token_required
def me():
    db = get_db()
    u = db.execute("SELECT id,nome,email,perfil,celula,criado_em FROM usuarios WHERE id=?",
                   (g.usuario["id"],)).fetchone()
    return jsonify(row_to_dict(u))


# ══════════════════════════════════════════
# ROTAS — USUÁRIOS (gestor/franqueadora)
# ══════════════════════════════════════════

@app.route("/api/usuarios", methods=["GET"])
@token_required
@perfil_required("gestor","franqueadora")
def listar_usuarios():
    db = get_db()
    users = db.execute("SELECT id,nome,email,perfil,celula,ativo,criado_em FROM usuarios ORDER BY nome").fetchall()
    return jsonify(rows_to_list(users))

@app.route("/api/usuarios", methods=["POST"])
@token_required
@perfil_required("gestor")
def criar_usuario():
    data = request.get_json() or {}
    nome   = data.get("nome","").strip()
    email  = data.get("email","").strip().lower()
    senha  = data.get("senha","")
    perfil = data.get("perfil","consultor")
    celula = data.get("celula")
    if not all([nome, email, senha]):
        return jsonify({"erro": "nome, email e senha obrigatórios"}), 400
    if perfil not in ("consultor","gestor","franqueadora"):
        return jsonify({"erro": "Perfil inválido"}), 400
    h = generate_password_hash(senha)
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO usuarios (nome,email,senha_hash,perfil,celula) VALUES (?,?,?,?,?)",
            (nome, email, h, perfil, celula)
        )
        db.commit()
        return jsonify({"id": cur.lastrowid, "mensagem": "Usuário criado com sucesso"}), 201
    except sqlite3.IntegrityError:
        return jsonify({"erro": "E-mail já cadastrado"}), 409


# ══════════════════════════════════════════
# ROTAS — LOJAS
# ══════════════════════════════════════════

@app.route("/api/lojas", methods=["GET"])
@token_required
def listar_lojas():
    db = get_db()
    perfil = g.usuario["perfil"]
    uid    = g.usuario["id"]
    # Consultor vê só as suas; gestor e franqueadora veem todas
    if perfil == "consultor":
        lojas = db.execute(
            "SELECT l.*, u.nome as consultor_nome FROM lojas l LEFT JOIN usuarios u ON l.consultor_id=u.id WHERE l.consultor_id=? ORDER BY l.nome",
            (uid,)
        ).fetchall()
    else:
        lojas = db.execute(
            "SELECT l.*, u.nome as consultor_nome FROM lojas l LEFT JOIN usuarios u ON l.consultor_id=u.id ORDER BY l.nome"
        ).fetchall()
    return jsonify(rows_to_list(lojas))

@app.route("/api/lojas", methods=["POST"])
@token_required
@perfil_required("gestor","franqueadora")
def criar_loja():
    data = request.get_json() or {}
    campos = ["nome","celula","cidade","gerente","telefone","status","fase","consultor_id"]
    vals   = {c: data.get(c) for c in campos}
    if not vals["nome"] or not vals["celula"]:
        return jsonify({"erro": "nome e celula obrigatórios"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO lojas (nome,celula,cidade,gerente,telefone,status,fase,consultor_id) VALUES (?,?,?,?,?,?,?,?)",
        (vals["nome"], vals["celula"], vals["cidade"], vals["gerente"],
         vals["telefone"], vals["status"] or "ok", vals["fase"] or 1, vals["consultor_id"])
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "mensagem": "Loja criada"}), 201

@app.route("/api/lojas/<int:loja_id>", methods=["PUT"])
@token_required
@perfil_required("gestor","franqueadora")
def atualizar_loja(loja_id):
    data = request.get_json() or {}
    db = get_db()
    campos = ["nome","celula","cidade","gerente","telefone","status","fase","consultor_id"]
    sets   = ", ".join([f"{c}=?" for c in campos if c in data])
    vals   = [data[c] for c in campos if c in data]
    if not sets:
        return jsonify({"erro": "Nenhum campo para atualizar"}), 400
    vals += [datetime.now().isoformat(), loja_id]
    db.execute(f"UPDATE lojas SET {sets}, atualizado_em=? WHERE id=?", vals)
    db.commit()
    return jsonify({"mensagem": "Loja atualizada"})


# ══════════════════════════════════════════
# ROTAS — VISITAS
# ══════════════════════════════════════════

@app.route("/api/visitas", methods=["GET"])
@token_required
def listar_visitas():
    db = get_db()
    perfil = g.usuario["perfil"]
    uid    = g.usuario["id"]
    loja_id = request.args.get("loja_id")

    base = """
        SELECT v.*, l.nome as loja_nome, l.celula as loja_celula,
               u.nome as consultor_nome
        FROM visitas v
        JOIN lojas l ON v.loja_id = l.id
        JOIN usuarios u ON v.consultor_id = u.id
    """
    if perfil == "consultor":
        where, params = "WHERE v.consultor_id=?", [uid]
    else:
        where, params = "WHERE 1=1", []

    if loja_id:
        where += " AND v.loja_id=?"
        params.append(loja_id)

    visitas = db.execute(f"{base} {where} ORDER BY v.data_visita DESC LIMIT 100", params).fetchall()
    return jsonify(rows_to_list(visitas))

@app.route("/api/visitas", methods=["POST"])
@token_required
def criar_visita():
    data = request.get_json() or {}
    loja_id = data.get("loja_id")
    if not loja_id:
        return jsonify({"erro": "loja_id obrigatório"}), 400
    db = get_db()
    # Consultor só pode criar visita nas suas lojas
    if g.usuario["perfil"] == "consultor":
        loja = db.execute("SELECT id FROM lojas WHERE id=? AND consultor_id=?",
                          (loja_id, g.usuario["id"])).fetchone()
        if not loja:
            return jsonify({"erro": "Loja não pertence a este consultor"}), 403
    cur = db.execute("""
        INSERT INTO visitas (loja_id, consultor_id, data_visita, fase, objetivo,
                             obs_campo, dor_gerente, dados_fin,
                             checks_json, lev_json, ferr_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        loja_id, g.usuario["id"],
        data.get("data_visita", datetime.now().date().isoformat()),
        data.get("fase", 1),
        data.get("objetivo"), data.get("obs_campo"),
        data.get("dor_gerente"), data.get("dados_fin"),
        json.dumps(data.get("checks", {})),
        json.dumps(data.get("lev", {})),
        json.dumps(data.get("ferr", {})),
    ))
    db.commit()
    return jsonify({"id": cur.lastrowid, "mensagem": "Visita criada"}), 201

@app.route("/api/visitas/<int:visita_id>", methods=["PUT"])
@token_required
def atualizar_visita(visita_id):
    data = request.get_json() or {}
    db = get_db()
    visita = db.execute("SELECT * FROM visitas WHERE id=?", (visita_id,)).fetchone()
    if not visita:
        return jsonify({"erro": "Visita não encontrada"}), 404
    if g.usuario["perfil"] == "consultor" and visita["consultor_id"] != g.usuario["id"]:
        return jsonify({"erro": "Sem permissão"}), 403

    campos_json = {"checks": "checks_json", "lev": "lev_json", "ferr": "ferr_json"}
    sets, vals = [], []

    for campo in ["fase","objetivo","obs_campo","dor_gerente","dados_fin","ia_output","yungas_texto","enviado_yungas","status"]:
        if campo in data:
            sets.append(f"{campo}=?"); vals.append(data[campo])

    for alias, col in campos_json.items():
        if alias in data:
            sets.append(f"{col}=?"); vals.append(json.dumps(data[alias]))

    if not sets:
        return jsonify({"erro": "Nada a atualizar"}), 400

    sets.append("atualizado_em=?"); vals.append(datetime.now().isoformat())
    vals.append(visita_id)
    db.execute(f"UPDATE visitas SET {', '.join(sets)} WHERE id=?", vals)
    db.commit()
    return jsonify({"mensagem": "Visita atualizada"})

@app.route("/api/visitas/<int:visita_id>", methods=["GET"])
@token_required
def detalhe_visita(visita_id):
    db = get_db()
    v = db.execute("""
        SELECT v.*, l.nome as loja_nome, l.celula as loja_celula, u.nome as consultor_nome
        FROM visitas v JOIN lojas l ON v.loja_id=l.id JOIN usuarios u ON v.consultor_id=u.id
        WHERE v.id=?
    """, (visita_id,)).fetchone()
    if not v:
        return jsonify({"erro": "Não encontrada"}), 404
    d = dict(v)
    for col in ["checks_json","lev_json","ferr_json"]:
        try: d[col] = json.loads(d[col] or "{}")
        except: d[col] = {}
    return jsonify(d)


# ══════════════════════════════════════════
# ROTAS — ACORDOS
# ══════════════════════════════════════════

@app.route("/api/acordos", methods=["GET"])
@token_required
def listar_acordos():
    db = get_db()
    perfil = g.usuario["perfil"]
    uid    = g.usuario["id"]
    status = request.args.get("status")          # aberto | concluido | cancelado
    loja_id = request.args.get("loja_id")

    base = """
        SELECT a.*, l.nome as loja_nome, u.nome as consultor_nome
        FROM acordos a
        JOIN lojas l ON a.loja_id = l.id
        JOIN usuarios u ON a.consultor_id = u.id
    """
    if perfil == "consultor":
        where, params = "WHERE a.consultor_id=?", [uid]
    else:
        where, params = "WHERE 1=1", []

    if status:
        where += " AND a.status=?"; params.append(status)
    if loja_id:
        where += " AND a.loja_id=?"; params.append(loja_id)

    acordos = db.execute(f"{base} {where} ORDER BY a.prazo ASC, a.criado_em DESC", params).fetchall()
    return jsonify(rows_to_list(acordos))

@app.route("/api/acordos", methods=["POST"])
@token_required
def criar_acordo():
    data = request.get_json() or {}
    loja_id = data.get("loja_id")
    desc    = data.get("descricao","").strip()
    if not loja_id or not desc:
        return jsonify({"erro": "loja_id e descricao obrigatórios"}), 400
    db = get_db()
    cur = db.execute("""
        INSERT INTO acordos (visita_id, loja_id, consultor_id, descricao, responsavel, prazo, fase)
        VALUES (?,?,?,?,?,?,?)
    """, (
        data.get("visita_id"), loja_id, g.usuario["id"],
        desc, data.get("responsavel"), data.get("prazo"), data.get("fase", 1)
    ))
    db.commit()
    return jsonify({"id": cur.lastrowid, "mensagem": "Acordo criado"}), 201

@app.route("/api/acordos/<int:acordo_id>", methods=["PUT"])
@token_required
def atualizar_acordo(acordo_id):
    data = request.get_json() or {}
    db = get_db()
    acordo = db.execute("SELECT * FROM acordos WHERE id=?", (acordo_id,)).fetchone()
    if not acordo:
        return jsonify({"erro": "Acordo não encontrado"}), 404
    if g.usuario["perfil"] == "consultor" and acordo["consultor_id"] != g.usuario["id"]:
        return jsonify({"erro": "Sem permissão"}), 403
    campos = ["descricao","responsavel","prazo","fase","status"]
    sets = [f"{c}=?" for c in campos if c in data]
    vals = [data[c] for c in campos if c in data]
    if not sets:
        return jsonify({"erro": "Nada a atualizar"}), 400
    vals += [datetime.now().isoformat(), acordo_id]
    db.execute(f"UPDATE acordos SET {', '.join(sets)}, atualizado_em=? WHERE id=?", vals)
    db.commit()
    return jsonify({"mensagem": "Acordo atualizado"})


# ══════════════════════════════════════════
# ROTAS — EVENTOS / LEMBRETES
# ══════════════════════════════════════════

@app.route("/api/eventos", methods=["GET"])
@token_required
def listar_eventos():
    db = get_db()
    uid = g.usuario["id"]
    mes = request.args.get("mes")  # formato YYYY-MM
    perfil = g.usuario["perfil"]

    if perfil == "consultor":
        where, params = "WHERE e.consultor_id=?", [uid]
    else:
        where, params = "WHERE 1=1", []

    if mes:
        where += " AND e.data_evento LIKE ?"; params.append(f"{mes}%")

    eventos = db.execute(f"""
        SELECT e.*, l.nome as loja_nome FROM eventos e
        LEFT JOIN lojas l ON e.loja_id=l.id
        {where} ORDER BY e.data_evento ASC
    """, params).fetchall()
    return jsonify(rows_to_list(eventos))

@app.route("/api/eventos", methods=["POST"])
@token_required
def criar_evento():
    data = request.get_json() or {}
    data_ev = data.get("data_evento","").strip()
    desc    = data.get("descricao","").strip()
    if not data_ev or not desc:
        return jsonify({"erro": "data_evento e descricao obrigatórios"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO eventos (loja_id, consultor_id, data_evento, descricao, tipo) VALUES (?,?,?,?,?)",
        (data.get("loja_id"), g.usuario["id"], data_ev, desc, data.get("tipo","lembrete"))
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "mensagem": "Evento criado"}), 201

@app.route("/api/eventos/<int:ev_id>", methods=["PUT"])
@token_required
def atualizar_evento(ev_id):
    data = request.get_json() or {}
    db = get_db()
    db.execute("UPDATE eventos SET concluido=? WHERE id=? AND consultor_id=?",
               (data.get("concluido", 1), ev_id, g.usuario["id"]))
    db.commit()
    return jsonify({"mensagem": "Evento atualizado"})


# ══════════════════════════════════════════
# ROTAS — DASHBOARD / KPIs
# ══════════════════════════════════════════

@app.route("/api/dashboard", methods=["GET"])
@token_required
def dashboard():
    db = get_db()
    uid    = g.usuario["id"]
    perfil = g.usuario["perfil"]
    hoje   = datetime.now().date().isoformat()
    mes    = datetime.now().strftime("%Y-%m")

    if perfil == "consultor":
        filtro_loja   = "WHERE consultor_id=?"
        filtro_visita = "WHERE v.consultor_id=?"
        filtro_acordo = "WHERE a.consultor_id=?"
        p = uid
    else:
        filtro_loja   = "WHERE 1=1"
        filtro_visita = "WHERE 1=1"
        filtro_acordo = "WHERE 1=1"
        p = None

    def q(sql, *args):
        return db.execute(sql, args if args else ()).fetchone()[0]

    total_lojas   = q(f"SELECT COUNT(*) FROM lojas {filtro_loja}", *([p] if p else []))
    visitas_mes   = q(f"SELECT COUNT(*) FROM visitas v {filtro_visita} AND v.data_visita LIKE ?",
                      *([p, f"{mes}%"] if p else [f"{mes}%"]))
    acordos_abertos = q(f"SELECT COUNT(*) FROM acordos a {filtro_acordo} AND a.status='aberto'",
                        *([p] if p else []))
    vencendo_hoje = q(f"SELECT COUNT(*) FROM acordos a {filtro_acordo} AND a.status='aberto' AND a.prazo<=?",
                      *([p, hoje] if p else [hoje]))

    # Distribuição de lojas por fase (para gestor/franqueadora)
    fases = {}
    if perfil != "consultor":
        rows = db.execute("SELECT fase, COUNT(*) as total FROM lojas GROUP BY fase").fetchall()
        fases = {str(r["fase"]): r["total"] for r in rows}

    # Últimas visitas
    if perfil == "consultor":
        ult = db.execute("""
            SELECT v.id, v.data_visita, v.fase, v.status, v.enviado_yungas,
                   l.nome as loja_nome, l.celula
            FROM visitas v JOIN lojas l ON v.loja_id=l.id
            WHERE v.consultor_id=? ORDER BY v.data_visita DESC LIMIT 5
        """, (uid,)).fetchall()
    else:
        ult = db.execute("""
            SELECT v.id, v.data_visita, v.fase, v.status, v.enviado_yungas,
                   l.nome as loja_nome, l.celula, u.nome as consultor_nome
            FROM visitas v JOIN lojas l ON v.loja_id=l.id JOIN usuarios u ON v.consultor_id=u.id
            ORDER BY v.data_visita DESC LIMIT 10
        """).fetchall()

    return jsonify({
        "total_lojas":      total_lojas,
        "visitas_mes":      visitas_mes,
        "acordos_abertos":  acordos_abertos,
        "vencendo_hoje":    vencendo_hoje,
        "fases":            fases,
        "ultimas_visitas":  rows_to_list(ult),
    })


# ══════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "versao": "2.0", "app": "Cresce Farma 360"})


# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    print(f"\n✦ Cresce Farma 360 — Backend iniciado na porta {port}")
    print(f"  Banco: {DB_PATH}")
    print(f"  Debug: {debug}\n")
    app.run(host="0.0.0.0", port=port, debug=debug)
    

# app.py
import os
import sqlite3
import io
import base64
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session, send_from_directory, abort
from flask_mail import Mail, Message

# Tentativa de import do SDK do Mercado Pago (opcional — se não instalado, rota de pagamento falhará)
try:
    import mercadopago
    MP_SDK_AVAILABLE = True
except Exception:
    MP_SDK_AVAILABLE = False

# Pillow para escrever na imagem (opcional se não disponível)
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

# --- Configurações via variáveis de ambiente (configure no Discloud) ---
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")          # token do Mercado Pago (requerido para pagamento)
EMAIL_USER = os.getenv("EMAIL_USER", "")                  # e-mail usado para enviar (ex: Gmail)
EMAIL_PASS = os.getenv("EMAIL_PASS", "")
SECRET_KEY = os.getenv("SECRET_KEY", "troque_essa_chave") # sessão do Flask
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "senha123")

# App Flask
app = Flask(__name__)
app.secret_key = SECRET_KEY

# Flask-Mail
app.config.update(
    MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com"),
    MAIL_PORT = int(os.getenv("MAIL_PORT", 587)),
    MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "True").lower() in ("true", "1", "yes"),
    MAIL_USERNAME = EMAIL_USER,
    MAIL_PASSWORD = EMAIL_PASS
)
mail = Mail(app)

# Banco SQLite
DB_PATH = os.getenv("DB_PATH", "ingressos.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ingressos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        evento TEXT NOT NULL,
        seq INTEGER NOT NULL,
        codigo TEXT NOT NULL UNIQUE,
        comprador_email TEXT NOT NULL,
        status TEXT NOT NULL,
        mp_payment_id TEXT,
        created_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()

init_db()

# Helper: gera código sequencial tipo LUAL0001
PREFIX = os.getenv("INGRESSO_PREFIX", "LUAL")

def next_seq():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT MAX(seq) as maxseq FROM ingressos")
    row = cur.fetchone()
    maxseq = row["maxseq"] if row and row["maxseq"] is not None else 0
    conn.close()
    return maxseq + 1

def format_code(seq):
    return f"{PREFIX}{seq:04d}"

# Rota pública: página principal (simples)
INDEX_HTML = """
<!doctype html>
<title>Venda de Ingressos</title>
<h1>Evento: Lual na Praia</h1>
<p>Data: 20/09/2025 — Valor: R$ 100,00</p>

<label>Seu e-mail:</label>
<input id="email" type="email" placeholder="seu@email.com">
<br><br>
<button onclick="comprar()">Comprar ingresso</button>

<p id="msg"></p>

<script>
async function comprar(){
  const email = document.getElementById("email").value;
  if(!email){ alert("Digite seu e-mail"); return; }
  document.getElementById("msg").innerText = "Criando preferência de pagamento...";
  const res = await fetch("/api/pagar", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({email: email, evento: "Lual na Praia", price: 100.0})
  });
  const data = await res.json();
  if(data.error){
    document.getElementById("msg").innerText = "Erro: " + data.error;
    return;
  }
  // redireciona para o checkout do Mercado Pago (init_point)
  if(data.init_point){
    window.location = data.init_point;
  } else {
    // fallback: mostra link
    document.getElementById("msg").innerHTML = "Abra este link para pagar: <a href='"+data.sandbox_init_point+"' target='_blank'>pagar</a>";
  }
}
</script>
"""

# Rota de sucesso (após pagar o cliente volta por aqui através do back_urls)
SUCCESS_HTML = """
<!doctype html>
<title>Pagamento</title>
<h1>Obrigado — pagamento iniciado</h1>
<p>Se o pagamento for aprovado você irá receber o ingresso por e-mail em breve.</p>
<p><a href="/">Voltar</a></p>
"""

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

@app.route("/sucesso")
def sucesso():
    return render_template_string(SUCCESS_HTML)

# Rota para criar preferência Mercado Pago
@app.route("/api/pagar", methods=["POST"])
def api_pagar():
    if not MP_SDK_AVAILABLE:
        return jsonify({"error": "SDK Mercado Pago (mercadopago) não instalado no servidor."}), 500
    data = request.get_json() or {}
    email = data.get("email")
    evento = data.get("evento", "Lual na Praia")
    price = float(data.get("price", 100.0))

    if not email:
        return jsonify({"error": "E-mail obrigatório"}), 400

    sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
    preference_data = {
        "items": [
            {
                "title": f"Ingresso - {evento}",
                "quantity": 1,
                "currency_id": "BRL",
                "unit_price": price
            }
        ],
        "payer": {"email": email},
        "back_urls": {
            "success": request.host_url.rstrip("/") + url_for("sucesso"),
            "failure": request.host_url.rstrip("/") + url_for("index")
        },
        "auto_return": "approved",
        "notification_url": request.host_url.rstrip("/") + url_for("webhook")  # webhook publico
    }

    try:
        preference = sdk.preference().create(preference_data)
        return jsonify(preference["response"])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Webhook para notificações do Mercado Pago
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(force=True, silent=True) or {}
    # Mercado Pago pode enviar com "topic" ou "type" e dentro data.id
    # We'll try to extract payment id robustly
    payment_id = None
    try:
        if "type" in payload and payload["type"] == "payment" and "data" in payload and "id" in payload["data"]:
            payment_id = payload["data"]["id"]
        elif "topic" in payload and payload["topic"] == "payment" and "id" in payload:
            payment_id = payload["id"]
        elif "data" in payload and isinstance(payload["data"], dict) and "id" in payload["data"]:
            payment_id = payload["data"]["id"]
        # Some MP notifications are form-encoded: ?topic=payment&id=XXX
        elif request.args.get("topic") == "payment" and request.args.get("id"):
            payment_id = request.args.get("id")
    except Exception:
        payment_id = None

    # If no payment_id, just return OK
    if not payment_id:
        return "OK", 200

    # Fetch payment details to check status
    if not MP_SDK_AVAILABLE:
        print("Webhook recebido, mas SDK Mercado Pago não está disponível.")
        return "OK", 200

    sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
    try:
        payment = sdk.payment().get(payment_id)
        payment_info = payment.get("response", {})
    except Exception as e:
        print("Erro ao consultar pagamento:", e)
        return "OK", 200

    status = payment_info.get("status")
    payer = payment_info.get("payer", {})
    payer_email = payer.get("email") or payment_info.get("payer_email") or "sem-email@local"
    # Apenas processa se aprovado
    if status and status.lower() == "approved":
        # Checa se já processado (payment_id único)
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM ingressos WHERE mp_payment_id = ?", (str(payment_id),))
        already = cur.fetchone()
        if already:
            conn.close()
            return "OK", 200

        seq = next_seq()
        codigo = format_code(seq)  # ex: LUAL0001
        now = datetime.utcnow().isoformat()

        # Gera arquivo de ingresso carimbado (se base existir e Pillow disponível)
        ingresso_dir = os.path.join("static", "ingressos")
        os.makedirs(ingresso_dir, exist_ok=True)
        generated_path = None

        base_path = os.path.join("static", "ingresso_base.png")  # sua arte base
        if PIL_AVAILABLE and os.path.exists(base_path):
            try:
                img = Image.open(base_path).convert("RGBA")
                draw = ImageDraw.Draw(img)
                # tenta carregar fonte ttf padrão em sistema; fallback para default
                try:
                    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                    font = ImageFont.truetype(font_path, 48)
                except Exception:
                    font = ImageFont.load_default()

                text = f"#{codigo}"
                # Posiciona no canto inferior direito com margem
                w, h = img.size
                tw, th = draw.textsize(text, font=font)
                x = w - tw - 40
                y = h - th - 40
                draw.text((x, y), text, font=font, fill=(255,255,255,255))
                generated_filename = f"{codigo}.png"
                generated_path = os.path.join(ingresso_dir, generated_filename)
                img.save(generated_path)
            except Exception as e:
                print("Erro ao gerar imagem carimbada:", e)
                generated_path = None
        else:
            # Sem Pillow ou sem base -> não gera imagem
            generated_path = None

        # Salva ingresso no banco
        cur.execute(
            "INSERT INTO ingressos (evento, seq, codigo, comprador_email, status, mp_payment_id, created_at) VALUES (?,?,?,?,?,?,?)",
            ("Lual na Praia", seq, codigo, payer_email, "pago", str(payment_id), now)
        )
        conn.commit()
        conn.close()

        # Envia e-mail com o ingresso (se gerado, anexa; senão anexa base ou só manda texto)
        try:
            msg = Message(f"Seu ingresso — {codigo}", sender=EMAIL_USER, recipients=[payer_email])
            msg.body = f"Obrigado pela compra!\n\nSeu ingresso: #{codigo}\n\nApresente este ingresso no evento."
            # Preferência: anexa imagem carimbada; se não existir, anexa base se presente
            if generated_path and os.path.exists(generated_path):
                with open(generated_path, "rb") as f:
                    msg.attach(f"{codigo}.png", "image/png", f.read())
            elif os.path.exists(base_path):
                with open(base_path, "rb") as f:
                    msg.attach("ingresso.png", "image/png", f.read())
                # também inclui o código no corpo do e-mail (já feito)
            # envia
            mail.send(msg)
        except Exception as e:
            print("Erro ao enviar e-mail:", e)
            # não falhamos o webhook por causa disso
    return "OK", 200

# Rota para baixar ingressos gerados (somente se existirem)
@app.route("/ingressos/<path:filename>")
def ingressos_files(filename):
    ingresso_dir = os.path.join("static", "ingressos")
    return send_from_directory(ingresso_dir, filename)

# --- Área administrativa simples (login + listagem) ---
ADMIN_LOGIN_HTML = """
<!doctype html>
<title>Admin - Login</title>
<h2>Login Admin</h2>
<form method="post" action="/admin/login">
  <label>Usuário:</label><input name="user"><br>
  <label>Senha:</label><input name="pass" type="password"><br>
  <button type="submit">Entrar</button>
</form>
"""

ADMIN_PANEL_HTML = """
<!doctype html>
<title>Admin - Ingressos</title>
<h1>Painel Admin</h1>
<p><a href="/admin/logout">Sair</a></p>
<table border="1" cellpadding="6" cellspacing="0">
  <tr><th>ID</th><th>Código</th><th>Seq</th><th>Email</th><th>Status</th><th>Pago em</th><th>MP Payment ID</th></tr>
  {% for i in ingressos %}
    <tr>
      <td>{{ i['id'] }}</td>
      <td>{{ i['codigo'] }}</td>
      <td>{{ i['seq'] }}</td>
      <td>{{ i['comprador_email'] }}</td>
      <td>{{ i['status'] }}</td>
      <td>{{ i['created_at'] }}</td>
      <td>{{ i['mp_payment_id'] }}</td>
    </tr>
  {% endfor %}
</table>
"""

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
        return render_template_string(ADMIN_LOGIN_HTML)
    user = request.form.get("user")
    pw = request.form.get("pass")
    if user == ADMIN_USER and pw == ADMIN_PASS:
        session["admin_logged"] = True
        return redirect(url_for("admin_panel"))
    return "Credenciais inválidas", 401

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged", None)
    return redirect(url_for("admin_login"))

@app.route("/admin")
@admin_required
def admin_panel():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ingressos ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return render_template_string(ADMIN_PANEL_HTML, ingressos=rows)

# Endpoint simples para checar status (ex: para debug)
@app.route("/api/ingressos")
@admin_required
def api_ingressos():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ingressos ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# Run
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

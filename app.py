import base64, io, os, uuid
from datetime import datetime, timedelta

import qrcode
from dotenv import load_dotenv
from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_file, url_for)
from flask_sqlalchemy import SQLAlchemy
from reportlab.lib.colors import HexColor, white
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from sqlalchemy.exc import IntegrityError

load_dotenv()
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev")
db_url = os.getenv("DATABASE_URL", "sqlite:///local.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 300}
db = SQLAlchemy(app)

# ---------- Configuração da peça ----------
EVENT = {k: os.getenv(f"EVENT_{k.upper()}", d) for k, d in {
    "title": "Peça Natalina", "date": "06/12/2026", "time": "A definir",
    "place": "Paróquia Santo Antônio do Limão"}.items()}
ROWS, SEATS_PER_ROW = "ABC", 14            # 3 fileiras x 14 = 42 lugares
PRICE_FRONT, PRICE_GENERAL = 10, 5
PRICE_POPCORN, PRICE_COKE = 2, 3
HOLD_MINUTES = 15                          # tempo para pagar antes de liberar assentos
ALL_SEATS = {f"{r}{n}" for r in ROWS for n in range(1, SEATS_PER_ROW + 1)}

# EDITE: elenco da peça
ACTORS = [
    {"name": "Nome do ator", "role": "Personagem"},
    {"name": "Nome da atriz", "role": "Personagem"},
]


# ---------- Modelos ----------
class Order(db.Model):
    id = db.Column(db.String(12), primary_key=True, default=lambda: uuid.uuid4().hex[:12])
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), nullable=False)
    phone = db.Column(db.String(30))
    popcorn = db.Column(db.Integer, default=0)
    coke = db.Column(db.Integer, default=0)
    total = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(10), default="pending")  # pending | paid | expired
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    tickets = db.relationship("Ticket", backref="order", cascade="all, delete-orphan")


class Ticket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.String(12), db.ForeignKey("order.id"), nullable=False)
    seat = db.Column(db.String(4), unique=True)           # None = entrada geral
    code = db.Column(db.String(32), unique=True, default=lambda: uuid.uuid4().hex)
    # Futuro app de leitura: adicionar aqui o campo "used_at"

    @property
    def label(self):
        return f"Lugar {self.seat}" if self.seat else "Entrada geral"


with app.app_context():
    db.create_all()


def release_expired():
    cutoff = datetime.utcnow() - timedelta(minutes=HOLD_MINUTES)
    for o in Order.query.filter(Order.status == "pending", Order.created_at < cutoff):
        o.status = "expired"
        Ticket.query.filter_by(order_id=o.id).delete()
    db.session.commit()


def taken_seats():
    return {t.seat for t in Ticket.query.filter(Ticket.seat.isnot(None))}


# ---------- Pix (BR Code estático) ----------
def _tlv(i, v):
    return f"{i}{len(v):02d}{v}"


def _crc16(s):
    crc = 0xFFFF
    for b in s.encode():
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return f"{crc:04X}"


def pix_payload(amount, txid):
    p = (_tlv("00", "01") + _tlv("01", "11")
         + _tlv("26", _tlv("00", "br.gov.bcb.pix") + _tlv("01", os.getenv("PIX_KEY", "")))
         + _tlv("52", "0000") + _tlv("53", "986") + _tlv("54", f"{amount:.2f}")
         + _tlv("58", "BR") + _tlv("59", os.getenv("PIX_NAME", "Luz do Limao")[:25])
         + _tlv("60", os.getenv("PIX_CITY", "SAO PAULO")[:15])
         + _tlv("62", _tlv("05", f"LUZ{txid}"[:25])) + "6304")
    return p + _crc16(p)


def qr_image(data):
    return qrcode.make(data, box_size=8, border=2)


def qr_data_uri(data):
    buf = io.BytesIO()
    qr_image(data).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------- Rotas ----------
@app.route("/")
def index():
    return render_template("index.html", e=EVENT)


@app.route("/sobre")
def sobre():
    return render_template("sobre.html", e=EVENT)


@app.route("/atores")
def atores():
    return render_template("atores.html", actors=ACTORS)


@app.route("/comprar", methods=["GET", "POST"])
def comprar():
    release_expired()
    if request.method == "POST":
        f = request.form
        seats = sorted({s for s in f.get("seats", "").split(",") if s in ALL_SEATS})
        num = lambda k, mx: max(0, min(mx, int(f.get(k) or 0))) if (f.get(k) or "0").isdigit() else 0
        general, popcorn, coke = num("general", 50), num("popcorn", 50), num("coke", 50)
        name, email = f.get("name", "").strip(), f.get("email", "").strip()
        if not name or "@" not in email:
            flash("Preencha nome e e-mail.")
        elif not seats and not general:
            flash("Escolha ao menos um ingresso.")
        elif set(seats) & taken_seats():
            flash("Algum lugar escolhido acabou de ser reservado. Escolha outro.")
        else:
            total = (len(seats) * PRICE_FRONT + general * PRICE_GENERAL
                     + popcorn * PRICE_POPCORN + coke * PRICE_COKE)   # calculado no servidor
            order = Order(name=name, email=email, phone=f.get("phone", "").strip(),
                          popcorn=popcorn, coke=coke, total=total)
            order.tickets = ([Ticket(seat=s) for s in seats]
                             + [Ticket(seat=None) for _ in range(general)])
            db.session.add(order)
            try:
                db.session.commit()
                return redirect(url_for("checkout", order_id=order.id))
            except IntegrityError:
                db.session.rollback()
                flash("Algum lugar escolhido acabou de ser reservado. Escolha outro.")
    return render_template("comprar.html", rows=ROWS, n=SEATS_PER_ROW, taken=taken_seats(),
                           prices=dict(front=PRICE_FRONT, general=PRICE_GENERAL,
                                       popcorn=PRICE_POPCORN, coke=PRICE_COKE))


@app.route("/checkout/<order_id>")
def checkout(order_id):
    o = db.get_or_404(Order, order_id)
    if o.status == "paid":
        return redirect(url_for("ingresso", order_id=o.id))
    if o.status == "expired":
        flash("Reserva expirada. Faça um novo pedido.")
        return redirect(url_for("comprar"))
    code = pix_payload(o.total, o.id)
    return render_template("checkout.html", o=o, code=code, qr=qr_data_uri(code),
                           minutes=HOLD_MINUTES)


@app.route("/status/<order_id>")
def status(order_id):
    return jsonify(status=db.get_or_404(Order, order_id).status)


@app.route("/ingresso/<order_id>")
def ingresso(order_id):
    o = db.get_or_404(Order, order_id)
    if o.status != "paid":
        return redirect(url_for("checkout", order_id=o.id))
    return render_template("ingresso.html", o=o, e=EVENT,
                           qrs=[(t, qr_data_uri(t.code)) for t in o.tickets])


@app.route("/ingresso/<order_id>.pdf")
def ingresso_pdf(order_id):
    o = db.get_or_404(Order, order_id)
    if o.status != "paid":
        abort(403)
    buf, W, H = io.BytesIO(), 100 * mm, 180 * mm
    c = canvas.Canvas(buf, pagesize=(W, H))
    for t in o.tickets:
        c.setFillColor(HexColor("#4a0d12")); c.rect(0, 0, W, H, fill=1, stroke=0)
        c.setFillColor(HexColor("#f4b942"))
        c.setFont("Helvetica-Bold", 22); c.drawCentredString(W / 2, H - 28 * mm, "Luz do Limão")
        c.setFillColor(white); c.setFont("Helvetica", 11)
        c.drawCentredString(W / 2, H - 36 * mm, EVENT["title"])
        c.drawCentredString(W / 2, H - 43 * mm, f'{EVENT["date"]} · {EVENT["time"]}')
        c.drawCentredString(W / 2, H - 50 * mm, EVENT["place"])
        c.setFillColor(white); c.roundRect(15 * mm, 38 * mm, 70 * mm, 70 * mm, 3 * mm, fill=1, stroke=0)
        c.drawImage(ImageReader(qr_image(t.code).get_image()), 18 * mm, 41 * mm, 64 * mm, 64 * mm)
        c.setFillColor(HexColor("#f4b942")); c.setFont("Helvetica-Bold", 16)
        c.drawCentredString(W / 2, 27 * mm, t.label)
        c.setFillColor(white); c.setFont("Helvetica", 9)
        c.drawCentredString(W / 2, 20 * mm, o.name)
        extras = [x for x in (f"{o.popcorn}x pipoca" if o.popcorn else "",
                              f"{o.coke}x Coca-Cola" if o.coke else "") if x]
        if extras:
            c.drawCentredString(W / 2, 14 * mm, "Retirar: " + " · ".join(extras))
        c.setFont("Helvetica", 7); c.drawCentredString(W / 2, 7 * mm, f"Pedido {o.id}")
        c.showPage()
    c.save(); buf.seek(0)
    return send_file(buf, mimetype="application/pdf", download_name=f"ingresso-{o.id}.pdf")


# ---------- Admin (confirmação manual do Pix) ----------
def _check_admin():
    if request.values.get("token") != os.getenv("ADMIN_TOKEN", "__off__"):
        abort(404)


@app.route("/admin")
def admin():
    _check_admin()
    orders = Order.query.order_by(Order.created_at.desc()).limit(200).all()
    return render_template("admin.html", orders=orders, token=request.args["token"])


@app.route("/admin/pagar/<order_id>", methods=["POST"])
def admin_pagar(order_id):
    _check_admin()
    o = db.get_or_404(Order, order_id)
    if o.status == "expired":                      # recria os lugares se ainda livres
        flash("Pedido expirado.")
    else:
        o.status = "paid"; db.session.commit()
    return redirect(url_for("admin", token=request.form["token"]))


if __name__ == "__main__":
    app.run(debug=True)

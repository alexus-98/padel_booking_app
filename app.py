from flask import Flask, render_template, request, jsonify, redirect, session
import sqlite3
import os
from dotenv import load_dotenv
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import psycopg2
from urllib.parse import urlparse

# Load environment variables from .env
load_dotenv()

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
COACH_EMAIL = os.getenv("COACH_EMAIL")
COACH_PASSWORD = os.getenv("COACH_PASSWORD")
SECRET_KEY = os.getenv("SECRET_KEY", "supersecretkey")

app = Flask(__name__)
app.secret_key = SECRET_KEY
DB_NAME = "database.db"


# ------------------ Database Setup ------------------

import os
import sqlite3
import psycopg2
from urllib.parse import urlparse

def get_db_connection():
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        # PostgreSQL on Render
        result = urlparse(db_url)
        conn = psycopg2.connect(
            database=result.path[1:],
            user=result.username,
            password=result.password,
            host=result.hostname,
            port=result.port,
        )
        conn.autocommit = True
        return conn
    else:
        # SQLite locally
        conn = sqlite3.connect("database.db")
        conn.row_factory = sqlite3.Row
        return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS slots (
            id SERIAL PRIMARY KEY,
            date TEXT,
            start_time TEXT,
            end_time TEXT,
            status TEXT DEFAULT 'available',
            client_name TEXT,
            client_email TEXT
        );
    ''')
    conn.commit()
    conn.close()


# ------------------ Email Helper ------------------

def send_email(to_email, subject, message):
    try:
        msg = MIMEMultipart()
        msg["From"] = EMAIL_USER
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(message, "html"))

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, to_email, msg.as_string())
    except Exception as e:
        print("Email sending error:", e)


# ------------------ Routes ------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/client")
def client_view():
    return render_template("client.html")


@app.route("/coach_login", methods=["GET", "POST"])
def coach_login():
    if request.method == "POST":
        password = request.form.get("password")
        if password == COACH_PASSWORD:
            session["coach_logged_in"] = True
            return redirect("/coach")
        else:
            return render_template("login.html", error="Incorrect password.")
    return render_template("login.html")


@app.route("/coach")
def coach_view():
    if not session.get("coach_logged_in"):
        return redirect("/coach_login")
    return render_template("coach.html")


@app.route("/logout")
def logout():
    session.pop("coach_logged_in", None)
    return redirect("/")


# ------------------ API Endpoints ------------------

@app.route("/api/slots")
def api_slots():
    """Return slots for calendar, with name shown only for coach."""
    only_available = request.args.get("only_available")
    is_coach = session.get("coach_logged_in")

    conn = get_db_connection()
    if only_available:
        slots = conn.execute("SELECT * FROM slots WHERE status='available'").fetchall()
    else:
        slots = conn.execute("SELECT * FROM slots").fetchall()
    conn.close()

    events = []
    for s in slots:
        if s["status"] == "booked":
            title = "Booked"
            if is_coach and s["client_name"]:
                title += f" — {s['client_name']}"
        else:
            title = "Available"

        events.append({
            "id": s["id"],
            "title": title,
            "start": f"{s['date']}T{s['start_time']}",
            "end": f"{s['date']}T{s['end_time']}",
            "color": "#0091ad" if s["status"] == "available" else "#ccc"
        })

    return jsonify(events)



@app.route("/api/add_slot", methods=["POST"])
def add_slot():
    """Coach adds a new available slot"""
    if not session.get("coach_logged_in"):
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    data = request.get_json()
    date = data.get("date")
    start = data.get("start_time")
    end = data.get("end_time")

    conn = get_db_connection()
    conn.execute(
        "INSERT INTO slots (date, start_time, end_time, status) VALUES (?, ?, ?, 'available')",
        (date, start, end),
    )
    conn.commit()
    conn.close()

    return jsonify({"success": True})


@app.route("/api/delete_slot/<int:slot_id>", methods=["DELETE"])
def delete_slot(slot_id):
    """Coach deletes a slot"""
    if not session.get("coach_logged_in"):
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    conn = get_db_connection()
    conn.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/unbook_slot/<int:slot_id>", methods=["POST"])
def unbook_slot(slot_id):
    """Coach cancels a booking and makes the slot available again"""
    if not session.get("coach_logged_in"):
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    conn = get_db_connection()
    conn.execute(
        "UPDATE slots SET status='available', client_name=NULL, client_email=NULL WHERE id=?",
        (slot_id,),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/book_slot", methods=["POST"])
def book_slot():
    """Client books an available slot"""
    data = request.get_json()
    slot_id = data.get("id")
    name = data.get("name")
    email = data.get("email")

    conn = get_db_connection()
    slot = conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()

    if not slot or slot["status"] == "booked":
        conn.close()
        return jsonify({"success": False, "error": "Slot unavailable"})

    # Update booking in DB
    conn.execute(
        "UPDATE slots SET status='booked', client_name=?, client_email=? WHERE id=?",
        (name, email, slot_id),
    )
    conn.commit()
    conn.close()

    # Send confirmation emails
    date = slot["date"]
    start = slot["start_time"]
    end = slot["end_time"]

    client_msg = f"""
    <h3>Booking Confirmation</h3>
    <p>Hi {name},</p>
    <p>Your padel training has been booked successfully!</p>
    <ul>
      <li><b>Date:</b> {date}</li>
      <li><b>Time:</b> {start} - {end}</li>
    </ul>
    <p>See you on court! 🥎</p>
    """
    coach_msg = f"""
    <h3>{name} wants to play!</h3>
    <p>{name} ({email}) has booked a session.</p>
    <ul>
      <li><b>Date:</b> {date}</li>
      <li><b>Time:</b> {start} - {end}</li>
    </ul>
    """

    send_email(email, "Padel Training Confirmation", client_msg)
    send_email(COACH_EMAIL, "New Padel Booking", coach_msg)

    return jsonify({"success": True})


# ------------------ Main ------------------

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
else:
    with app.app_context():
        init_db()


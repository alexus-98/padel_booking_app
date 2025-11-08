from flask import Flask, render_template, request, redirect, jsonify, session, url_for
import sqlite3
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

# --- Load environment variables ---
load_dotenv()

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
COACH_EMAIL = os.getenv("COACH_EMAIL")
COACH_PASSWORD = os.getenv("COACH_PASSWORD")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey")  # required for sessions
DB_NAME = "database.db"

# --- Database setup ---
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            start_time TEXT,
            end_time TEXT,
            status TEXT DEFAULT 'available',
            client_name TEXT,
            client_email TEXT
        )
    ''')
    conn.commit()
    conn.close()

# --- Authenticated coach view ---
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/coach_login', methods=['GET', 'POST'])
def coach_login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == COACH_PASSWORD:
            session['coach_logged_in'] = True
            return redirect('/coach')
        else:
            return render_template('login.html', error="Incorrect password.")
    return render_template('login.html')

@app.route('/coach')
def coach_view():
    if not session.get('coach_logged_in'):
        return redirect('/coach_login')
    return render_template('coach.html')

@app.route('/logout')
def logout():
    session.pop('coach_logged_in', None)
    return redirect('/coach_login')

@app.route('/client')
def client_view():
    return render_template('client.html')

# --- API endpoints ---
@app.route('/api/slots')
def get_slots():
    only_available = request.args.get('only_available')
    client_email = request.args.get('client_email')

    conn = get_db_connection()
    if only_available:
        slots = conn.execute("SELECT * FROM slots WHERE status='available'").fetchall()
    elif client_email:
        slots = conn.execute("""
            SELECT * FROM slots 
            WHERE status='available' OR (status='booked' AND client_email=?)
        """, (client_email,)).fetchall()
    else:
        slots = conn.execute("SELECT * FROM slots").fetchall()
    conn.close()

    events = []
    for s in slots:
        start = f"{s['date']}T{s['start_time']}"
        end = f"{s['date']}T{s['end_time']}"
        color = "#28a745" if s["status"] == "available" else "#dc3545"
        title = "Available" if s["status"] == "available" else f"Booked ({s['client_name']})"
        events.append({
            "id": s["id"],
            "title": title,
            "start": start,
            "end": end,
            "color": color
        })
    return jsonify(events)

@app.route('/api/add_slot', methods=['POST'])
def add_slot():
    data = request.json
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO slots (date, start_time, end_time, status) VALUES (?, ?, ?, 'available')",
        (data["date"], data["start_time"], data["end_time"])
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/book_slot', methods=['POST'])
def book_slot():
    data = request.json
    slot_id = data.get("id")
    name = data.get("name")
    email = data.get("email")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        UPDATE slots
        SET status='booked', client_name=?, client_email=?
        WHERE id=?
    """, (name, email, slot_id))
    conn.commit()

    c.execute("SELECT date, start_time, end_time FROM slots WHERE id=?", (slot_id,))
    slot = c.fetchone()
    conn.close()

    if slot:
        # Send email to client
        send_confirmation_email(name, email, slot["date"], slot["start_time"], slot["end_time"])
        # Send email to coach
        send_confirmation_email_to_coach(name, email, slot["date"], slot["start_time"], slot["end_time"])

    return jsonify({"success": True})

@app.route('/api/delete_slot', methods=['POST'])
def delete_slot():
    data = request.json
    slot_id = data.get("id")
    conn = get_db_connection()
    conn.execute("DELETE FROM slots WHERE id=?", (slot_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/api/cancel_booking', methods=['POST'])
def cancel_booking():
    data = request.json
    slot_id = data.get("id")
    conn = get_db_connection()
    conn.execute("""
        UPDATE slots
        SET status='available', client_name=NULL, client_email=NULL
        WHERE id=?
    """, (slot_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# --- Email functions ---
def send_confirmation_email(name, recipient_email, date, start, end):
    subject = "Padel Lesson Booking Confirmation"
    body = f"""
    Hi {name},

    Your padel lesson has been successfully booked!

    📅 Date: {date}
    🕒 Time: {start} - {end}
    📍 Location: Padlovnia

    See you on court! 💪

    Best,
    Your Coach
    """
    send_email(recipient_email, subject, body)

def send_confirmation_email_to_coach(name, client_email, date, start, end):
    subject = "New Padel Lesson Booking"
    body = f"""
    Hi Coach,

    A new padel lesson has been booked.

    👤 Client: {name}
    📧 Email: {client_email}
    📅 Date: {date}
    🕒 Time: {start} - {end}
    """
    send_email(COACH_EMAIL, subject, body)

def send_email(to_email, subject, body):
    msg = MIMEMultipart()
    msg["From"] = EMAIL_USER
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
        print(f"✅ Email sent to {to_email}")
    except Exception as e:
        print(f"⚠️ Email error: {e}")

if __name__ == "__main__":
    init_db()
    app.run(debug=True)

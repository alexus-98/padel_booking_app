from flask import Flask, render_template, request, jsonify, redirect, session
import sqlite3
import os
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras
from urllib.parse import urlparse
import traceback
import sys
import threading

# SendGrid imports
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

# Load environment variables
load_dotenv()

EMAIL_USER = os.getenv("EMAIL_USER")  # must be verified sender in SendGrid
EMAIL_PASS = os.getenv("EMAIL_PASS")  # unused now, but kept in case
COACH_EMAIL = os.getenv("COACH_EMAIL")
COACH_PASSWORD = os.getenv("COACH_PASSWORD")
SECRET_KEY = os.getenv("SECRET_KEY", "supersecretkey")
SENDGRID_KEY = os.getenv("SENDGRID_API_KEY")

app = Flask(__name__)
app.secret_key = SECRET_KEY


# ------------------ DB Helpers ------------------

def is_postgres():
    return bool(os.getenv("DATABASE_URL"))


def get_raw_connection():
    """Return either a RealDictCursor Postgres conn or SQLite conn."""
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        result = urlparse(db_url)
        conn = psycopg2.connect(
            database=result.path[1:],
            user=result.username,
            password=result.password,
            host=result.hostname,
            port=result.port,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        conn.autocommit = False
        return conn
    else:
        conn = sqlite3.connect("database.db", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn


def placeholder():
    """Return correct parameter placeholder depending on DB."""
    return "%s" if is_postgres() else "?"


def run_query(conn, query, params=None, fetch=None):
    """Unified query executor for both SQLite and Postgres."""
    params = params or ()
    try:
        if is_postgres():
            cur = conn.cursor()
            cur.execute(query, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None
        else:
            cur = conn.execute(query, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None
    except Exception:
        traceback.print_exc(file=sys.stdout)
        raise


def commit_and_close(conn):
    try:
        conn.commit()
    except Exception:
        traceback.print_exc(file=sys.stdout)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ------------------ DB Initialization ------------------

def init_db():
    conn = get_raw_connection()
    try:
        cur = conn.cursor() if is_postgres() else conn

        if is_postgres():
            # Postgres schema
            cur.execute("""
                CREATE TABLE IF NOT EXISTS slots (
                    id SERIAL PRIMARY KEY,
                    date TEXT,
                    start_time TEXT,
                    end_time TEXT,
                    status TEXT DEFAULT 'available',
                    client_name TEXT,
                    client_email TEXT
                );
            """)
        else:
            # SQLite schema
            cur.execute("""
                CREATE TABLE IF NOT EXISTS slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT,
                    start_time TEXT,
                    end_time TEXT,
                    status TEXT DEFAULT 'available',
                    client_name TEXT,
                    client_email TEXT
                );
            """)

        conn.commit()
    except Exception:
        traceback.print_exc(file=sys.stdout)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ------------------ SendGrid Email Helper ------------------

def _sendgrid_send(to_email, subject, message_html):
    """Actual SendGrid API call inside a thread."""
    if not SENDGRID_KEY:
        print("SENDGRID_API_KEY missing — skipping email")
        return
    try:
        sg = SendGridAPIClient(SENDGRID_KEY)
        email = Mail(
            from_email=EMAIL_USER,
            to_emails=to_email,
            subject=subject,
            html_content=message_html,
        )
        sg.send(email)
    except Exception as e:
        print("SendGrid email error:", e)
        traceback.print_exc(file=sys.stdout)


def send_email(to_email, subject, message_html):
    """Fire-and-forget async email sending."""
    try:
        t = threading.Thread(
            target=_sendgrid_send,
            args=(to_email, subject, message_html),
            daemon=True
        )
        t.start()
    except Exception as e:
        print("Error starting email thread:", e)


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


# ------------------ API ------------------

@app.route("/api/slots")
def api_slots():
    only_available = request.args.get("only_available")
    is_coach = session.get("coach_logged_in")

    conn = get_raw_connection()

    try:
        if only_available:
            q = "SELECT * FROM slots WHERE status='available' ORDER BY date, start_time"
            slots = run_query(conn, q, fetch="all")
        else:
            q = "SELECT * FROM slots ORDER BY date, start_time"
            slots = run_query(conn, q, fetch="all")
    except Exception:
        traceback.print_exc(file=sys.stdout)
        try:
            conn.close()
        except Exception:
            pass
        return jsonify([])

    events = []
    for s in slots:
        status = s["status"]
        if status == "booked":
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
            "color": "#0091ad" if status == "available" else "#ccc",
        })

    try:
        conn.close()
    except Exception:
        pass

    return jsonify(events)


@app.route("/api/add_slot", methods=["POST"])
def add_slot():
    if not session.get("coach_logged_in"):
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    data = request.get_json() or {}
    date = data.get("date")
    start = data.get("start_time")
    end = data.get("end_time")

    if not date or not start or not end:
        return jsonify({"success": False, "error": "Missing fields"}), 400

    conn = get_raw_connection()
    ph = placeholder()

    try:
        q = f"INSERT INTO slots (date, start_time, end_time, status) VALUES ({ph}, {ph}, {ph}, 'available')"
        run_query(conn, q, (date, start, end))
        commit_and_close(conn)
        return jsonify({"success": True})
    except Exception:
        traceback.print_exc(file=sys.stdout)
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({"success": False}), 500


@app.route("/api/delete_slot/<int:slot_id>", methods=["DELETE"])
def delete_slot(slot_id):
    if not session.get("coach_logged_in"):
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    conn = get_raw_connection()
    ph = placeholder()

    try:
        q = f"DELETE FROM slots WHERE id={ph}"
        run_query(conn, q, (slot_id,))
        commit_and_close(conn)
        return jsonify({"success": True})
    except Exception:
        traceback.print_exc(file=sys.stdout)
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({"success": False}), 500


@app.route("/api/unbook_slot/<int:slot_id>", methods=["POST"])
def unbook_slot(slot_id):
    if not session.get("coach_logged_in"):
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    conn = get_raw_connection()
    ph = placeholder()

    try:
        q = f"UPDATE slots SET status='available', client_name=NULL, client_email=NULL WHERE id={ph}"
        run_query(conn, q, (slot_id,))
        commit_and_close(conn)
        return jsonify({"success": True})
    except Exception:
        traceback.print_exc(file=sys.stdout)
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({"success": False}), 500


@app.route("/api/book_slot", methods=["POST"])
def book_slot():
    data = request.get_json() or {}

    try:
        slot_id = int(data.get("id"))
    except:
        return jsonify({"success": False, "error": "Invalid slot ID"}), 400

    name = data.get("name")
    email = data.get("email")

    if not name or not email:
        return jsonify({"success": False, "error": "Missing info"}), 400

    conn = get_raw_connection()
    ph = placeholder()

    try:
        # SELECT slot
        q_get = f"SELECT * FROM slots WHERE id={ph}"
        slot = run_query(conn, q_get, (slot_id,), fetch="one")

        if not slot or slot["status"] == "booked":
            conn.close()
            return jsonify({"success": False, "error": "Slot unavailable"}), 200

        # UPDATE booking
        q_update = f"""
            UPDATE slots SET status='booked',
            client_name={ph}, client_email={ph}
            WHERE id={ph}
        """
        run_query(conn, q_update, (name, email, slot_id))
        commit_and_close(conn)

        # Prepare confirmation messages
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
        <h3>{name} booked a session</h3>
        <p>{name} ({email}) booked a training session.</p>
        <ul>
          <li><b>Date:</b> {date}</li>
          <li><b>Time:</b> {start} - {end}</li>
        </ul>
        """

        # Fire-and-forget emails (do NOT block)
        send_email(email, "Padel Training Confirmation", client_msg)
        send_email(COACH_EMAIL, "New Padel Booking", coach_msg)

        return jsonify({"success": True})
    except Exception:
        traceback.print_exc(file=sys.stdout)
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({"success": False, "error": "DB error"}), 500


# ------------------ Launch ------------------

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
else:
    with app.app_context():
        init_db()

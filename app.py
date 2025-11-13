from flask import Flask, render_template, request, jsonify, redirect, session
import sqlite3
import os
from dotenv import load_dotenv
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import psycopg2
import psycopg2.extras
from urllib.parse import urlparse
import traceback
import sys

# Load environment variables from .env
load_dotenv()

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
COACH_EMAIL = os.getenv("COACH_EMAIL")
COACH_PASSWORD = os.getenv("COACH_PASSWORD")
SECRET_KEY = os.getenv("SECRET_KEY", "supersecretkey")

app = Flask(__name__)
app.secret_key = SECRET_KEY


# ------------------ DB abstraction helpers ------------------

def is_postgres():
    return bool(os.getenv("DATABASE_URL"))


def get_raw_connection():
    """
    Return a raw DB connection object for either SQLite or Postgres.
    For Postgres we set cursor_factory=RealDictCursor so fetches return dict-like rows.
    """
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
        # We'll manage commits explicitly; autocommit helps some deployments
        conn.autocommit = False
        return conn
    else:
        conn = sqlite3.connect("database.db", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn


def placeholder():
    """Return the correct SQL parameter placeholder for the current DB."""
    return "%s" if is_postgres() else "?"


def run_query(conn, query, params=None, fetch=None):
    """
    Unified query executor.

    - conn: raw connection from get_raw_connection()
    - query: parameterized SQL string (use placeholder() to format)
    - params: tuple/list of params
    - fetch: None | "one" | "all"
    """
    params = params or ()
    try:
        if is_postgres():
            # psycopg2: use cursor
            cur = conn.cursor()
            cur.execute(query, params)
            if fetch == "one":
                res = cur.fetchone()
            elif fetch == "all":
                res = cur.fetchall()
            else:
                res = None
            return res
        else:
            # sqlite3: connection.execute returns a cursor-like object
            cur = conn.execute(query, params)
            if fetch == "one":
                return cur.fetchone()
            elif fetch == "all":
                return cur.fetchall()
            else:
                return None
    except Exception:
        # re-raise after printing stack for debugging (Render logs)
        traceback.print_exc(file=sys.stdout)
        raise


def commit_and_close(conn):
    try:
        if is_postgres():
            conn.commit()
        else:
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


# ------------------ DB initialization ------------------

def init_db():
    conn = get_raw_connection()
    ph = placeholder()
    try:
        cur = conn.cursor() if is_postgres() else conn
        if is_postgres():
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
        # print to logs but don't crash the app
        print("Email sending error:", e, file=sys.stdout)
        traceback.print_exc(file=sys.stdout)


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

    conn = get_raw_connection()
    try:
        if only_available:
            q = "SELECT * FROM slots WHERE status='available' ORDER BY date, start_time"
            slots = run_query(conn, q, fetch="all")
        else:
            q = "SELECT * FROM slots ORDER BY date, start_time"
            slots = run_query(conn, q, fetch="all")
    except Exception as e:
        traceback.print_exc(file=sys.stdout)
        try:
            conn.close()
        except Exception:
            pass
        return jsonify([])

    # convert sqlite3.Row to dict-like for uniform access
    events = []
    for s in slots:
        # s may be sqlite3.Row or RealDictRow; both support item access by key
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
            "color": "#0091ad" if status == "available" else "#ccc"
        })

    try:
        conn.close()
    except Exception:
        pass

    return jsonify(events)


@app.route("/api/add_slot", methods=["POST"])
def add_slot():
    """Coach adds a new available slot"""
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
        return jsonify({"success": False, "error": "DB error"}), 500


@app.route("/api/delete_slot/<int:slot_id>", methods=["DELETE"])
def delete_slot(slot_id):
    """Coach deletes a slot"""
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
        return jsonify({"success": False, "error": "DB error"}), 500


@app.route("/api/unbook_slot/<int:slot_id>", methods=["POST"])
def unbook_slot(slot_id):
    """Coach cancels a booking and makes the slot available again"""
    if not session.get("coach_logged_in"):
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    conn = get_raw_connection()
    ph = placeholder()
    try:
        # set client_name and client_email to NULL via parameters (None)
        q = f"UPDATE slots SET status='available', client_name={ph}, client_email={ph} WHERE id={ph}"
        run_query(conn, q, (None, None, slot_id))
        commit_and_close(conn)
        return jsonify({"success": True})
    except Exception:
        traceback.print_exc(file=sys.stdout)
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({"success": False, "error": "DB error"}), 500


@app.route("/api/book_slot", methods=["POST"])
def book_slot():
    """Client books an available slot"""
    data = request.get_json() or {}
    try:
        slot_id = int(data.get("id"))
    except Exception:
        return jsonify({"success": False, "error": "Invalid slot id"}), 400

    name = data.get("name")
    email = data.get("email")

    if not name or not email:
        return jsonify({"success": False, "error": "Missing name/email"}), 400

    conn = get_raw_connection()
    ph = placeholder()
    try:
        # SELECT the slot
        q_select = f"SELECT * FROM slots WHERE id={ph}"
        slot = run_query(conn, q_select, (slot_id,), fetch="one")

        if not slot or slot["status"] == "booked":
            try:
                conn.close()
            except Exception:
                pass
            return jsonify({"success": False, "error": "Slot unavailable"})

        # UPDATE to booked
        q_update = f"UPDATE slots SET status='booked', client_name={ph}, client_email={ph} WHERE id={ph}"
        run_query(conn, q_update, (name, email, slot_id))
        commit_and_close(conn)

        # Send confirmation emails using the originally selected slot info
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

        # Fire-and-forget emails (errors only print to logs)
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


# ------------------ Main ------------------

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
else:
    with app.app_context():
        init_db()

from flask import Flask, render_template, request, redirect, session, url_for
from werkzeug.utils import secure_filename
import os
import mysql.connector
from db_config import get_connection

app = Flask(__name__)
app.secret_key = "CHANGE_THIS_TO_A_RANDOM_SECRET_KEY"

# ------------------- PHOTO UPLOAD SETTINGS -------------------
UPLOAD_FOLDER = os.path.join('static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER


def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def require_login() -> bool:
    return bool(session.get("logged_in"))


# ------------------- DB MIGRATION HELPERS -------------------
def safe_exec(cur, sql: str, params=None) -> bool:
    try:
        cur.execute(sql, params or ())
        return True
    except mysql.connector.Error as e:
        # Don't crash app for old DBs; print so you can see it in terminal
        print("⚠️ MIGRATION SKIPPED:", e)
        print("   SQL:", sql)
        return False


def add_column_if_missing(cur, table: str, column: str, col_def: str):
    cur.execute("""
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
    """, (table, column))
    if cur.fetchone()[0] == 0:
        safe_exec(cur, f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def add_index_if_missing(cur, table: str, index_name: str, create_sql: str):
    cur.execute("""
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND INDEX_NAME = %s
    """, (table, index_name))
    if cur.fetchone()[0] == 0:
        safe_exec(cur, create_sql)


def ensure_schema():
    """
    Creates missing tables + upgrades old tables by adding missing columns/indexes.
    """
    conn = get_connection()
    cur = conn.cursor()

    # ---------- TABLES ----------
    safe_exec(cur, """
        CREATE TABLE IF NOT EXISTS batches (
            batch_id INT AUTO_INCREMENT PRIMARY KEY,
            batch_name VARCHAR(100) NOT NULL,
            start_date DATE NULL,
            notes VARCHAR(255) NULL
        )
    """)

    # Use TIMESTAMP (more compatible than DATETIME DEFAULT CURRENT_TIMESTAMP on older MySQL)
    safe_exec(cur, """
        CREATE TABLE IF NOT EXISTS goats (
            goat_id INT AUTO_INCREMENT PRIMARY KEY,
            breed VARCHAR(100) NOT NULL,
            age FLOAT NOT NULL DEFAULT 0,
            weight FLOAT NOT NULL DEFAULT 0,
            barcode VARCHAR(100) NULL,
            photo VARCHAR(255) NULL,
            batch_id INT NULL,
            purchase_price DECIMAL(10,2) NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE SET NULL
        )
    """)

    safe_exec(cur, """
        CREATE TABLE IF NOT EXISTS goat_sales (
            id INT AUTO_INCREMENT PRIMARY KEY,
            goat_id INT NOT NULL,
            sale_date DATE NOT NULL,
            sold_price DECIMAL(10,2) NOT NULL,
            buyer_name VARCHAR(120) NULL,
            notes VARCHAR(255) NULL,
            FOREIGN KEY (goat_id) REFERENCES goats(goat_id) ON DELETE CASCADE
        )
    """)

    safe_exec(cur, """
        CREATE TABLE IF NOT EXISTS goat_weights (
            id INT AUTO_INCREMENT PRIMARY KEY,
            goat_id INT NOT NULL,
            record_date DATE NOT NULL,
            weight FLOAT NOT NULL,
            FOREIGN KEY (goat_id) REFERENCES goats(goat_id) ON DELETE CASCADE
        )
    """)

    safe_exec(cur, """
        CREATE TABLE IF NOT EXISTS medical_history (
            id INT AUTO_INCREMENT PRIMARY KEY,
            goat_id INT NOT NULL,
            record_date DATE NOT NULL,
            description VARCHAR(255) NOT NULL,
            FOREIGN KEY (goat_id) REFERENCES goats(goat_id) ON DELETE CASCADE
        )
    """)

    safe_exec(cur, """
        CREATE TABLE IF NOT EXISTS settings (
            k VARCHAR(50) PRIMARY KEY,
            v VARCHAR(255)
        )
    """)

    # ---------- MIGRATIONS (UPGRADE OLD DBs) ----------
    # goats upgrades
    add_column_if_missing(cur, "goats", "purchase_price", "DECIMAL(10,2) NOT NULL DEFAULT 0")
    add_column_if_missing(cur, "goats", "created_at", "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP")

    # goat_sales upgrades (YOUR ERROR IS HERE: old table missing id)
    add_column_if_missing(cur, "goat_sales", "id", "INT NOT NULL AUTO_INCREMENT PRIMARY KEY")
    add_column_if_missing(cur, "goat_sales", "buyer_name", "VARCHAR(120) NULL")
    add_column_if_missing(cur, "goat_sales", "notes", "VARCHAR(255) NULL")

    # Prevent double-selling: unique goat_id in goat_sales
    add_index_if_missing(
        cur,
        "goat_sales",
        "uniq_goat_sale",
        "ALTER TABLE goat_sales ADD UNIQUE KEY uniq_goat_sale (goat_id)"
    )

    conn.commit()
    cur.close()
    conn.close()


def has_column_cached(table: str, column: str) -> bool:
    cache_key = f"col_{table}_{column}"
    if cache_key in session:
        return bool(session[cache_key])

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
    """, (table, column))
    ok = cur.fetchone()[0] > 0
    cur.close()
    conn.close()
    session[cache_key] = bool(ok)
    return bool(ok)


# ------------------- LOGIN -------------------
@app.route('/', methods=['GET', 'POST'])
def login():
    ensure_schema()

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if username == "admin" and password == "admin123":
            session["logged_in"] = True
            session["finance_unlocked"] = False
            session.pop("unlock_error", None)
            return redirect(url_for('dashboard'))
        else:
            return render_template('login.html', error="Invalid credentials")

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ------------------- DASHBOARD -------------------
@app.route('/dashboard')
def dashboard():
    if not require_login():
        return redirect(url_for('login'))

    ensure_schema()

    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT batch_id FROM batches ORDER BY batch_id ASC")
    batches = cur.fetchall()
    if not batches:
        cur.execute("INSERT INTO batches (batch_name) VALUES ('Batch 1')")
        conn.commit()

    cur.execute("SELECT COUNT(*) AS total_batches FROM batches")
    total_batches = int(cur.fetchone()["total_batches"])

    cur.execute("SELECT COUNT(*) AS total_goats FROM goats")
    total_goats = int(cur.fetchone()["total_goats"])

    cur.execute("SELECT COUNT(*) AS sold_goats FROM goat_sales")
    sold_goats = int(cur.fetchone()["sold_goats"])

    goats_in_hand = max(0, total_goats - sold_goats)

    trend_labels, trend_values = [], []
    if has_column_cached("goats", "created_at"):
        cur.execute("""
            SELECT DATE_FORMAT(created_at, '%Y-%m') AS ym, COUNT(*) AS c
            FROM goats
            GROUP BY ym
            ORDER BY ym ASC
        """)
        rows = cur.fetchall()
        trend_labels = [r["ym"] for r in rows]
        trend_values = [int(r["c"]) for r in rows]

    finance_preview = None
    if session.get("finance_unlocked"):
        cur.execute("SELECT COALESCE(SUM(sold_price),0) AS revenue FROM goat_sales")
        revenue = float(cur.fetchone()["revenue"])

        cur.execute("""
            SELECT COALESCE(SUM(g.purchase_price),0) AS cost_sold
            FROM goats g
            JOIN goat_sales s ON s.goat_id = g.goat_id
        """)
        cost_sold = float(cur.fetchone()["cost_sold"])

        finance_preview = {"revenue": revenue, "cost_sold": cost_sold, "profit": revenue - cost_sold}

    cur.close()
    conn.close()

    unlock_error = bool(session.pop("unlock_error", False))

    return render_template(
        "dashboard_batches.html",
        total_batches=total_batches,
        total_goats=total_goats,
        sold_goats=sold_goats,
        goats_in_hand=goats_in_hand,
        trend_labels=trend_labels,
        trend_values=trend_values,
        finance_unlocked=session.get("finance_unlocked", False),
        finance=finance_preview,
        unlock_error=unlock_error
    )


# ------------------- UNLOCK FINANCE -------------------
@app.route('/unlock_finance', methods=['POST'])
def unlock_finance():
    if not require_login():
        return redirect(url_for('login'))

    pin = request.form.get("pin", "").strip()
    FINANCE_PIN = "7788"

    if pin == FINANCE_PIN:
        session["finance_unlocked"] = True
        session["unlock_error"] = False
    else:
        session["finance_unlocked"] = False
        session["unlock_error"] = True

    return redirect(url_for('dashboard'))


# ------------------- FINANCE PAGE -------------------
@app.route('/finance')
def finance():
    if not require_login():
        return redirect(url_for('login'))

    if not session.get("finance_unlocked"):
        return redirect(url_for('dashboard'))

    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT COUNT(*) AS total_goats FROM goats")
    total_goats = int(cur.fetchone()["total_goats"])

    cur.execute("SELECT COUNT(*) AS sold_goats FROM goat_sales")
    sold_goats = int(cur.fetchone()["sold_goats"])

    goats_in_hand = max(0, total_goats - sold_goats)

    cur.execute("SELECT COALESCE(SUM(sold_price),0) AS revenue FROM goat_sales")
    revenue = float(cur.fetchone()["revenue"])

    cur.execute("""
        SELECT COALESCE(SUM(g.purchase_price),0) AS cost_sold
        FROM goats g
        JOIN goat_sales s ON s.goat_id = g.goat_id
    """)
    cost_sold = float(cur.fetchone()["cost_sold"])

    profit = revenue - cost_sold

    cur.execute("""
        SELECT COALESCE(b.batch_name,'No Batch') AS batch_name,
               COUNT(s.goat_id) AS sold_count,
               COALESCE(SUM(s.sold_price),0) AS revenue,
               COALESCE(SUM(g.purchase_price),0) AS cost,
               (COALESCE(SUM(s.sold_price),0) - COALESCE(SUM(g.purchase_price),0)) AS profit
        FROM goat_sales s
        JOIN goats g ON g.goat_id = s.goat_id
        LEFT JOIN batches b ON b.batch_id = g.batch_id
        GROUP BY COALESCE(b.batch_name,'No Batch')
        ORDER BY batch_name ASC
    """)
    batch_rows = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "finance.html",
        total_goats=total_goats,
        sold_goats=sold_goats,
        goats_in_hand=goats_in_hand,
        revenue=revenue,
        cost_sold=cost_sold,
        profit=profit,
        batch_rows=batch_rows
    )


# ------------------- BATCH SUMMARY -------------------
@app.route('/batch_summary')
def batch_summary():
    if not require_login():
        return redirect(url_for('login'))

    ensure_schema()

    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT COUNT(*) AS total_goats FROM goats")
    total_goats = int(cur.fetchone()["total_goats"])

    cur.execute("""
        SELECT
            b.batch_id,
            b.batch_name,
            b.start_date,
            b.notes,
            COUNT(g.goat_id) AS total_in_batch,
            COALESCE(SUM(CASE WHEN s.goat_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS sold_in_batch,
            (COUNT(g.goat_id) - COALESCE(SUM(CASE WHEN s.goat_id IS NOT NULL THEN 1 ELSE 0 END), 0)) AS in_hand_in_batch,
            COALESCE(AVG(g.age),0) AS avg_age,
            COALESCE(AVG(g.weight),0) AS avg_weight
        FROM batches b
        LEFT JOIN goats g ON g.batch_id = b.batch_id
        LEFT JOIN goat_sales s ON s.goat_id = g.goat_id
        GROUP BY b.batch_id, b.batch_name, b.start_date, b.notes
        ORDER BY b.batch_id ASC
    """)
    rows_raw = cur.fetchall()

    rows = []
    for r in rows_raw:
        c = int(r["total_in_batch"] or 0)
        pct = (c / total_goats * 100.0) if total_goats > 0 else 0.0

        avg_age = float(r["avg_age"] or 0)
        years = int(avg_age)
        months = int((avg_age - years) * 12)
        if months == 12:
            years += 1
            months = 0

        rows.append({
            "batch_id": r["batch_id"],
            "batch_name": r["batch_name"],
            "start_date": r["start_date"],
            "notes": r["notes"],
            "count": c,
            "sold_count": int(r["sold_in_batch"] or 0),
            "in_hand": int(r["in_hand_in_batch"] or 0),
            "pct": pct,
            "avg_age_y": years,
            "avg_age_m": months,
            "avg_weight": round(float(r["avg_weight"] or 0), 1)
        })

    cur.close()
    conn.close()

    return render_template("batch_summary.html", total_goats=total_goats, rows=rows)


# ------------------- BATCH DETAILS -------------------
@app.route('/batch/<int:batch_id>')
def batch_details(batch_id):
    if not require_login():
        return redirect(url_for('login'))

    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT * FROM batches WHERE batch_id=%s", (batch_id,))
    batch = cur.fetchone()
    if not batch:
        cur.close()
        conn.close()
        return "Batch not found"

    cur.execute("""
        SELECT
            g.goat_id,
            g.breed,
            g.age,
            g.weight,
            g.barcode,
            g.photo,
            g.purchase_price,
            CASE WHEN s.goat_id IS NULL THEN 0 ELSE 1 END AS is_sold
        FROM goats g
        LEFT JOIN goat_sales s ON s.goat_id = g.goat_id
        WHERE g.batch_id=%s
        ORDER BY g.goat_id ASC
    """, (batch_id,))
    goats = cur.fetchall()

    goat_list = []
    for g in goats:
        age_float = float(g["age"] or 0)
        years = int(age_float)
        months = int((age_float - years) * 12)
        if months == 12:
            years += 1
            months = 0

        cur.execute("""
            SELECT weight
            FROM goat_weights
            WHERE goat_id=%s
            ORDER BY record_date DESC, id DESC
            LIMIT 1
        """, (g["goat_id"],))
        lw = cur.fetchone()
        latest_weight = float(lw["weight"]) if lw else float(g["weight"] or 0)

        goat_list.append({
            "id": g["goat_id"],
            "breed": g["breed"],
            "age_years": years,
            "age_months": months,
            "latest_weight": latest_weight,
            "barcode": g.get("barcode"),
            "photo": g.get("photo"),
            "purchase_price": float(g.get("purchase_price") or 0),
            "is_sold": bool(g.get("is_sold"))
        })

    cur.close()
    conn.close()

    return render_template("batch_details.html", batch=batch, goats=goat_list, count=len(goat_list))


# ------------------- ADD BATCH -------------------
@app.route('/add_batch', methods=['GET', 'POST'])
def add_batch():
    if not require_login():
        return redirect(url_for('login'))

    if request.method == 'POST':
        batch_name = request.form.get('batch_name')
        start_date = request.form.get('start_date')
        notes = request.form.get('notes')

        if not batch_name:
            return "Batch name required"

        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO batches (batch_name, start_date, notes) VALUES (%s, %s, %s)",
            (batch_name, start_date if start_date else None, notes if notes else None)
        )
        conn.commit()
        cur.close()
        conn.close()
        return redirect(url_for('dashboard'))

    return render_template('add_batch.html')


# ------------------- ADD GOAT -------------------
@app.route('/add_goat', methods=['GET', 'POST'])
def add_goat():
    if not require_login():
        return redirect(url_for('login'))

    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT batch_id, batch_name FROM batches ORDER BY batch_id ASC")
    batches = cur.fetchall()

    if request.method == 'POST':
        breed = request.form.get('breed')
        years = request.form.get('age_years')
        months = request.form.get('age_months')
        weight = request.form.get('weight')
        batch_id = request.form.get('batch_id')
        purchase_price = request.form.get('purchase_price', '0')

        if not breed or years in (None, '') or months in (None, '') or weight in (None, ''):
            cur.close()
            conn.close()
            return "Form data missing"

        try:
            years = int(years)
            months = int(months)
            weight = float(weight)
            purchase_price = float(purchase_price or 0)
        except (TypeError, ValueError):
            cur.close()
            conn.close()
            return "Invalid form data"

        if years < 0 or months < 0 or months > 11 or weight <= 0 or purchase_price < 0:
            cur.close()
            conn.close()
            return "Invalid form data"

        age = years + months / 12

        if not batch_id:
            batch_id = batches[0]["batch_id"] if batches else None

        cur2 = conn.cursor()
        cur2.execute(
            "INSERT INTO goats (breed, age, weight, batch_id, purchase_price) VALUES (%s, %s, %s, %s, %s)",
            (breed, age, weight, batch_id, purchase_price)
        )
        conn.commit()
        cur2.close()

        cur.close()
        conn.close()
        return redirect(url_for('dashboard'))

    cur.close()
    conn.close()
    return render_template('add_goat.html', batches=batches)


# ------------------- SELL GOAT (FIXED: DO NOT SELECT id) -------------------
@app.route('/sell_goat/<int:goat_id>', methods=['GET', 'POST'])
def sell_goat(goat_id):
    if not require_login():
        return redirect(url_for('login'))

    ensure_schema()

    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT goat_id, breed, purchase_price, batch_id FROM goats WHERE goat_id=%s", (goat_id,))
    goat = cur.fetchone()
    if not goat:
        cur.close()
        conn.close()
        return "Goat not found"

    # ✅ FIX: don't select "id" because old DB may not have it
    cur.execute("SELECT goat_id FROM goat_sales WHERE goat_id=%s LIMIT 1", (goat_id,))
    already = cur.fetchone()
    if already and request.method == "GET":
        cur.close()
        conn.close()
        return "This goat is already SOLD."

    if request.method == 'POST':
        sale_date = request.form.get('sale_date')
        sold_price = request.form.get('sold_price')
        buyer_name = request.form.get('buyer_name')
        notes = request.form.get('notes')

        if not sale_date or not sold_price:
            cur.close()
            conn.close()
            return "Sale date and sold price required"

        sold_price = float(sold_price)
        if sold_price < 0:
            cur.close()
            conn.close()
            return "Invalid sold price"

        try:
            cur.execute("""
                INSERT INTO goat_sales (goat_id, sale_date, sold_price, buyer_name, notes)
                VALUES (%s,%s,%s,%s,%s)
            """, (goat_id, sale_date, sold_price, buyer_name or None, notes or None))
            conn.commit()
        except mysql.connector.IntegrityError:
            cur.close()
            conn.close()
            return "Already sold. Cannot sell again."

        cur.close()
        conn.close()
        return redirect(url_for('finance') if session.get("finance_unlocked") else url_for('dashboard'))

    cur.close()
    conn.close()
    return render_template("sell_goat.html", goat=goat, purchase_price=float(goat.get("purchase_price") or 0))


# ------------------- GOAT DETAILS -------------------
@app.route('/goat/<int:goat_id>')
def goat_details(goat_id):
    if not require_login():
        return redirect(url_for('login'))

    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("""
        SELECT g.goat_id, g.breed, g.age, g.weight, g.barcode, g.photo, g.purchase_price,
               b.batch_id, b.batch_name
        FROM goats g
        LEFT JOIN batches b ON g.batch_id=b.batch_id
        WHERE g.goat_id=%s
    """, (goat_id,))
    goat = cur.fetchone()
    if not goat:
        cur.close()
        conn.close()
        return "Goat not found"

    # ✅ Also don't depend on id existing
    cur.execute("""
        SELECT goat_id, sale_date, sold_price, buyer_name, notes
        FROM goat_sales
        WHERE goat_id=%s
        LIMIT 1
    """, (goat_id,))
    sale = cur.fetchone()

    cur.close()
    conn.close()
    return render_template("goat_details.html", goat=goat, sale=sale)


# ======================================================================
# ✅ ROUTES FOR BUTTONS IN batch_details.html
# ======================================================================

@app.route('/upload_photo/<int:goat_id>', methods=['GET', 'POST'])
def upload_photo(goat_id):
    if not require_login():
        return redirect(url_for('login'))

    ensure_schema()
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT goat_id, breed, photo, batch_id FROM goats WHERE goat_id=%s", (goat_id,))
    goat = cur.fetchone()
    if not goat:
        cur.close()
        conn.close()
        return "Goat not found"

    cur.execute("SELECT goat_id FROM goat_sales WHERE goat_id=%s LIMIT 1", (goat_id,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return "This goat is SOLD. Photo change is locked."

    if request.method == 'POST':
        if 'photo' not in request.files:
            cur.close()
            conn.close()
            return "No file part"

        file = request.files['photo']
        if file.filename == '':
            cur.close()
            conn.close()
            return "No selected file"

        if not allowed_file(file.filename):
            cur.close()
            conn.close()
            return "Invalid file type"

        filename = secure_filename(file.filename)
        _, ext = os.path.splitext(filename)
        filename = f"goat_{goat_id}{ext.lower()}"

        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(save_path)

        cur2 = conn.cursor()
        cur2.execute("UPDATE goats SET photo=%s WHERE goat_id=%s", (filename, goat_id))
        conn.commit()
        cur2.close()

        cur.close()
        conn.close()
        return redirect(url_for('batch_details', batch_id=goat.get("batch_id") or 1))

    cur.close()
    conn.close()

    return f"""
    <!DOCTYPE html>
    <html><head><meta charset="UTF-8"><title>Upload Photo</title>
    <style>
      body{{font-family:Segoe UI,Tahoma; background:#f5f7fa; padding:22px}}
      .card{{max-width:520px;margin:auto;background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:16px;box-shadow:0 14px 30px rgba(0,0,0,.10)}}
      h2{{margin:0 0 10px 0;color:#2e7d32}}
      input{{width:100%;padding:10px;border:1px solid #e5e7eb;border-radius:12px}}
      button{{margin-top:12px;padding:10px 14px;border:0;border-radius:12px;background:#6d28d9;color:#fff;font-weight:900;cursor:pointer}}
      a{{display:inline-block;margin-top:12px;color:#2e7d32;text-decoration:none;font-weight:900}}
    </style>
    </head><body>
      <div class="card">
        <h2>Upload Photo - Goat #{goat_id}</h2>
        <form method="POST" enctype="multipart/form-data">
          <input type="file" name="photo" accept="image/*" required>
          <button type="submit">Save Photo</button>
        </form>
        <a href="{url_for('dashboard')}">← Back</a>
      </div>
    </body></html>
    """


@app.route('/edit_goat/<int:goat_id>', methods=['GET', 'POST'])
def edit_goat(goat_id):
    if not require_login():
        return redirect(url_for('login'))

    ensure_schema()

    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT * FROM goats WHERE goat_id=%s", (goat_id,))
    goat = cur.fetchone()
    if not goat:
        cur.close()
        conn.close()
        return "Goat not found"

    cur.execute("SELECT goat_id FROM goat_sales WHERE goat_id=%s LIMIT 1", (goat_id,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return "This goat is SOLD. Edit is locked."

    cur.execute("SELECT batch_id, batch_name FROM batches ORDER BY batch_id ASC")
    batches = cur.fetchall()

    if request.method == 'POST':
        breed = request.form.get("breed", "").strip()
        age_years = request.form.get("age_years", "0")
        age_months = request.form.get("age_months", "0")
        weight = request.form.get("weight", "0")
        batch_id = request.form.get("batch_id") or None
        purchase_price = request.form.get("purchase_price", "0")
        barcode = request.form.get("barcode") or None

        try:
            y = int(age_years)
            m = int(age_months)
            w = float(weight)
            p = float(purchase_price or 0)
        except Exception:
            cur.close()
            conn.close()
            return "Invalid data"

        if y < 0 or m < 0 or m > 11 or w <= 0 or p < 0:
            cur.close()
            conn.close()
            return "Invalid data"

        age = y + m / 12

        cur2 = conn.cursor()
        cur2.execute("""
            UPDATE goats
            SET breed=%s, age=%s, weight=%s, batch_id=%s, purchase_price=%s, barcode=%s
            WHERE goat_id=%s
        """, (breed, age, w, batch_id, p, barcode, goat_id))
        conn.commit()
        cur2.close()

        cur.close()
        conn.close()
        return redirect(url_for('goat_details', goat_id=goat_id))

    cur.close()
    conn.close()
    return render_template("edit_goat.html", goat=goat, batches=batches)


@app.route('/delete_goat/<int:goat_id>', methods=['POST'])
def delete_goat(goat_id):
    if not require_login():
        return redirect(url_for('login'))

    ensure_schema()

    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT goat_id FROM goat_sales WHERE goat_id=%s LIMIT 1", (goat_id,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return "This goat is SOLD. Delete is locked."

    cur.execute("SELECT batch_id FROM goats WHERE goat_id=%s", (goat_id,))
    r = cur.fetchone()
    batch_id = r["batch_id"] if r else None

    cur2 = conn.cursor()
    cur2.execute("DELETE FROM goats WHERE goat_id=%s", (goat_id,))
    conn.commit()
    cur2.close()

    cur.close()
    conn.close()

    if batch_id:
        return redirect(url_for('batch_details', batch_id=batch_id))
    return redirect(url_for('dashboard'))


@app.route('/add_weight/<int:goat_id>', methods=['GET', 'POST'])
def add_weight(goat_id):
    if not require_login():
        return redirect(url_for('login'))

    ensure_schema()

    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT goat_id, breed, batch_id FROM goats WHERE goat_id=%s", (goat_id,))
    goat = cur.fetchone()
    if not goat:
        cur.close()
        conn.close()
        return "Goat not found"

    cur.execute("SELECT goat_id FROM goat_sales WHERE goat_id=%s LIMIT 1", (goat_id,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return "This goat is SOLD. Weight update is locked."

    if request.method == "POST":
        record_date = request.form.get("record_date")
        weight = request.form.get("weight")

        if not record_date or not weight:
            cur.close()
            conn.close()
            return "Date and weight required"

        try:
            weight = float(weight)
        except Exception:
            cur.close()
            conn.close()
            return "Invalid weight"

        if weight <= 0:
            cur.close()
            conn.close()
            return "Invalid weight"

        cur2 = conn.cursor()
        cur2.execute("""
            INSERT INTO goat_weights (goat_id, record_date, weight)
            VALUES (%s, %s, %s)
        """, (goat_id, record_date, weight))
        conn.commit()
        cur2.close()

        cur.close()
        conn.close()
        return redirect(url_for('goat_details', goat_id=goat_id))

    cur.close()
    conn.close()

    return f"""
    <!DOCTYPE html>
    <html><head><meta charset="UTF-8"><title>Add Weight</title>
    <style>
      body{{font-family:Segoe UI,Tahoma; background:#f5f7fa; padding:22px}}
      .card{{max-width:520px;margin:auto;background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:16px;box-shadow:0 14px 30px rgba(0,0,0,.10)}}
      h2{{margin:0 0 10px 0;color:#2e7d32}}
      label{{display:block;margin-top:12px;font-weight:900}}
      input{{width:100%;padding:10px;border:1px solid #e5e7eb;border-radius:12px}}
      button{{margin-top:12px;padding:10px 14px;border:0;border-radius:12px;background:#ffb300;color:#111;font-weight:900;cursor:pointer}}
      a{{display:inline-block;margin-top:12px;color:#2e7d32;text-decoration:none;font-weight:900}}
    </style>
    </head><body>
      <div class="card">
        <h2>Add Weight - Goat #{goat_id}</h2>
        <form method="POST">
          <label>Date</label>
          <input type="date" name="record_date" required>
          <label>Weight (kg)</label>
          <input type="number" step="0.1" name="weight" required>
          <button type="submit">Save</button>
        </form>
        <a href="{url_for('dashboard')}">← Back</a>
      </div>
    </body></html>
    """


if __name__ == "__main__":
    app.run(debug=True)
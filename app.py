import os
import sqlite3
import secrets
import smtplib
import ssl

from datetime import datetime
from email.message import EmailMessage

from flask import (
    Flask,
    render_template,
    jsonify,
    request,
    redirect,
    url_for,
    session
)

from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)


# =========================================================
# APPLICATION CONFIGURATION
# =========================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

DATABASE = os.path.join(
    BASE_DIR,
    "database.db"
)

app = Flask(
    __name__,
    template_folder=os.path.join(
        BASE_DIR,
        "templates"
    ),
    static_folder=os.path.join(
        BASE_DIR,
        "static"
    )
)

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    secrets.token_hex(32)
)


# =========================================================
# DATABASE HELPERS
# =========================================================

def get_db_connection():

    conn = sqlite3.connect(
        DATABASE,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA foreign_keys = ON"
    )

    conn.execute(
        "PRAGMA busy_timeout = 30000"
    )

    return conn


def current_datetime():

    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def safe_int(
    value,
    default=0
):

    try:

        return int(value)

    except (
        TypeError,
        ValueError
    ):

        return default


def clean_text(value):

    if value is None:

        return ""

    return str(value).strip()


def ensure_master_user_location(conn, user_name="", location_name=""):
    """Create missing master user/location rows from saved asset form data.

    The separate Users / Locations UI is intentionally not shown. Asset User
    and Location values remain the source entered by the operator, while this
    helper keeps the master tables populated for issue-user validation and APIs.
    """
    user_name = clean_text(user_name)
    location_name = clean_text(location_name)
    now = current_datetime()
    location_id = None

    if location_name:
        existing_location = conn.execute(
            "SELECT id FROM locations WHERE LOWER(TRIM(location_name)) = LOWER(TRIM(?)) LIMIT 1",
            (location_name,)
        ).fetchone()
        if existing_location:
            location_id = existing_location["id"]
        else:
            cursor = conn.execute(
                "INSERT INTO locations (location_name, location_code, active, created_at) VALUES (?, '', 1, ?)",
                (location_name, now)
            )
            location_id = cursor.lastrowid

    if user_name:
        existing_user = conn.execute(
            "SELECT id, location_id FROM users WHERE LOWER(TRIM(full_name)) = LOWER(TRIM(?)) AND active = 1 ORDER BY id LIMIT 1",
            (user_name,)
        ).fetchone()
        if existing_user:
            if location_id is not None and not existing_user["location_id"]:
                conn.execute(
                    "UPDATE users SET location_id = ? WHERE id = ?",
                    (location_id, existing_user["id"])
                )
        else:
            conn.execute(
                "INSERT INTO users (full_name, employee_code, email, department_id, location_id, active, created_at) VALUES (?, '', '', NULL, ?, 1, ?)",
                (user_name, location_id, now)
            )


# =========================================================
# REFILL DATA HELPERS
# =========================================================
# =========================================================
# EDIT ASSET FROM MANAGE SAVED ASSETS
# =========================================================

@app.route("/api/assets/edit", methods=["POST"])
def edit_asset_api():

    conn = None
    updated_toner = None
    toner_id = None
    old_quantity = 0
    new_quantity = 0
    minimum_stock = 5
    stock_changed = False
    toner_created = False
    asset_id = 0

    try:
        data = request.get_json(silent=True) or {}

        asset_id = safe_int(
            data.get("asset_id", data.get("id")),
            0
        )

        asset_no = clean_text(data.get("asset_no"))
        asset_description = clean_text(
            data.get("asset_description")
        )
        asset_user = clean_text(data.get("user"))
        asset_location = clean_text(data.get("location"))
        toner_cartridge = clean_text(
            data.get("toner_cartridge")
        )
        # Toner model/name is stored in toners. Keep the old request format
        # working by falling back to the type field when a model is omitted.
        toner_model = clean_text(
            data.get("toner_model") or data.get("model_name") or toner_cartridge
        )
        colour = clean_text(data.get("colour"))

        stock = safe_int(data.get("stock", 0), -1)
        minimum_stock = safe_int(
            data.get("minimum_stock", 5),
            5
        )

        # -------------------------------------------------
        # VALIDATION
        # -------------------------------------------------

        if asset_id <= 0:
            return jsonify({
                "success": False,
                "error": "Invalid Asset ID."
            }), 400

        if not asset_no:
            return jsonify({
                "success": False,
                "error": "Asset No is required."
            }), 400

        if not asset_description:
            return jsonify({
                "success": False,
                "error": "Asset Description is required."
            }), 400

        if not asset_user:
            return jsonify({
                "success": False,
                "error": "User is required."
            }), 400

        if not asset_location:
            return jsonify({
                "success": False,
                "error": "Location is required."
            }), 400

        if stock < 0:
            return jsonify({
                "success": False,
                "error": "Stock cannot be negative."
            }), 400

        if minimum_stock < 1:
            minimum_stock = 5

        # Toner type can be blank for an asset-only record.
        # Accept free-form custom values such as new cartridge names.
        if toner_cartridge:
            toner_cartridge = toner_cartridge.strip()
            if not toner_cartridge:
                toner_cartridge = ""

        # Colour is optional.
        allowed_colours = {
            "black": "Black",
            "cyan": "Cyan",
            "magenta": "Magenta",
            "yellow": "Yellow",
            "normal": "Normal"
        }

        if colour:
            colour_key = colour.lower()

            if colour_key not in allowed_colours:
                return jsonify({
                    "success": False,
                    "error": (
                        "Colour must be Black, Cyan, "
                        "Magenta, Yellow, or Normal."
                    )
                }), 400

            colour = allowed_colours[colour_key]

        # -------------------------------------------------
        # FIND ASSET AND LINKED TONER
        # -------------------------------------------------

        conn = get_db_connection()

        asset = conn.execute(
            """
            SELECT id
            FROM assets
            WHERE id = ?
            LIMIT 1
            """,
            (asset_id,)
        ).fetchone()

        if asset is None:
            return jsonify({
                "success": False,
                "error": "Asset not found."
            }), 404

        toner = conn.execute(
            """
            SELECT
                id,
                quantity,
                minimum_stock,
                issued_quantity
            FROM toners
            WHERE asset_id = ?
              AND (
                  deleted_at IS NULL
                  OR deleted_at = ''
              )
            ORDER BY id DESC
            LIMIT 1
            """,
            (asset_id,)
        ).fetchone()

        # Don't remove the type from an existing active toner record.
        # The record must remain identifiable in Issue / Return.
        if toner is not None and not toner_cartridge:
            return jsonify({
                "success": False,
                "error": (
                    "This asset has active toner inventory. "
                    "Select Toner, Drum, or Normal."
                )
            }), 400

        # -------------------------------------------------
        # DUPLICATE CHECK
        # Same Asset No is allowed for different colours.
        # -------------------------------------------------

        duplicate = conn.execute(
            """
            SELECT id
            FROM assets
            WHERE id <> ?
              AND LOWER(TRIM(asset_name)) =
                  LOWER(TRIM(?))
              AND LOWER(TRIM(COALESCE(toner_cartridge, ''))) =
                  LOWER(TRIM(?))
              AND LOWER(TRIM(COALESCE(colour, ''))) =
                  LOWER(TRIM(?))
            LIMIT 1
            """,
            (
                asset_id,
                asset_no,
                toner_cartridge,
                colour
            )
        ).fetchone()

        if duplicate is not None:
            return jsonify({
                "success": False,
                "error": (
                    "Another record already uses this "
                    "Asset No, Type, and Colour."
                )
            }), 409

        # -------------------------------------------------
        # UPDATE MASTER USER / LOCATION IF NEEDED
        # -------------------------------------------------

        ensure_master_user_location(
            conn,
            asset_user,
            asset_location
        )

        now = current_datetime()

        # Asset-only rows do not have toner stock.
        saved_stock = stock if toner_cartridge else 0

        # -------------------------------------------------
        # UPDATE ASSET
        # -------------------------------------------------

        conn.execute(
            """
            UPDATE assets
            SET
                asset_name = ?,
                asset_type = ?,
                user = ?,
                location = ?,
                toner_cartridge = ?,
                colour = ?,
                stock = ?,
                minimum_stock = ?
            WHERE id = ?
            """,
            (
                asset_no,
                asset_description,
                asset_user,
                asset_location,
                toner_cartridge,
                colour,
                saved_stock,
                minimum_stock,
                asset_id
            )
        )

        # -------------------------------------------------
        # UPDATE EXISTING TONER OR CREATE ONE
        # -------------------------------------------------

        if toner is not None:

            toner_id = toner["id"]
            old_quantity = safe_int(
                toner["quantity"],
                0
            )
            new_quantity = saved_stock
            stock_changed = old_quantity != new_quantity

            conn.execute(
                """
                UPDATE toners
                SET
                    toner_model = ?,
                    colour = ?,
                    quantity = ?,
                    printer = ?,
                    minimum_stock = ?,
                    asset_no = ?
                WHERE id = ?
                """,
                (
                    toner_model,
                    colour,
                    new_quantity,
                    asset_description,
                    minimum_stock,
                    asset_no,
                    toner_id
                )
            )

            # Log a manual stock adjustment in transaction history.
            if stock_changed:
                conn.execute(
                    """
                    INSERT INTO toner_transactions
                    (
                        toner_id,
                        toner_model,
                        transaction_type,
                        quantity,
                        previous_quantity,
                        new_quantity,
                        transaction_date,
                        transaction_time,
                        remarks
                    )
                    VALUES (?, ?, 'ADJUST', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        toner_id,
                        toner_cartridge,
                        abs(new_quantity - old_quantity),
                        old_quantity,
                        new_quantity,
                        now[:10],
                        now[11:16],
                        "Stock adjusted while editing asset"
                    )
                )

        elif toner_cartridge:

            # This asset previously had no active toner record.
            cursor = conn.execute(
                """
                INSERT INTO toners
                (
                    toner_model,
                    colour,
                    quantity,
                    printer,
                    minimum_stock,
                    remarks,
                    issued_quantity,
                    created_at,
                    asset_id,
                    asset_no,
                    deleted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, NULL)
                """,
                (
                    toner_model,
                    colour,
                    saved_stock,
                    asset_description,
                    minimum_stock,
                    "Toner created while editing asset",
                    now,
                    asset_id,
                    asset_no
                )
            )

            toner_id = cursor.lastrowid
            toner_created = True
            new_quantity = saved_stock
            stock_changed = True

            conn.execute(
                """
                INSERT INTO toner_transactions
                (
                    toner_id,
                    toner_model,
                    transaction_type,
                    quantity,
                    previous_quantity,
                    new_quantity,
                    transaction_date,
                    transaction_time,
                    remarks
                )
                VALUES (?, ?, 'ADD', ?, 0, ?, ?, ?, ?)
                """,
                (
                    toner_id,
                    toner_model,
                    saved_stock,
                    saved_stock,
                    now[:10],
                    now[11:16],
                    "Toner created while editing asset"
                )
            )

        # -------------------------------------------------
        # COMMIT AND LOAD UPDATED RECORD
        # -------------------------------------------------

        conn.commit()

        if toner_id is not None:
            updated_toner = conn.execute(
                """
                SELECT
                    t.*,
                    a.asset_name AS asset_no
                FROM toners t
                LEFT JOIN assets a
                    ON a.id = t.asset_id
                WHERE t.id = ?
                LIMIT 1
                """,
                (toner_id,)
            ).fetchone()

    except Exception as error:

        if conn is not None:
            conn.rollback()

        app.logger.exception("EDIT ASSET ERROR")

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500

    finally:
        if conn is not None:
            conn.close()

    # -----------------------------------------------------
    # EMAIL ALERT WHEN EDITING CHANGES STOCK STATUS
    # -----------------------------------------------------

    email_result = {
        "sent": False,
        "reason": "Stock status did not require an email alert."
    }

    if updated_toner is not None and stock_changed:

        if toner_created:
            if new_quantity == 0:
                email_result = send_toner_alert(
                    updated_toner,
                    1
                )
            elif get_stock_status(
                new_quantity,
                minimum_stock
            ) in ("Understock", "Danger", "Overstock"):
                email_result = send_toner_alert(
                    updated_toner,
                    10
                )

        else:
            email_result = send_toner_alert(
                updated_toner,
                old_quantity
            )

    # -----------------------------------------------------
    # RESPONSE
    # -----------------------------------------------------

    return jsonify({
        "success": True,
        "message": "Asset updated successfully.",
        "asset": {
            "id": asset_id,
            "asset_no": asset_no,
            "asset_description": asset_description,
            "user": asset_user,
            "location": asset_location,
            "toner_cartridge": toner_cartridge,
            "toner_model": toner_model,
            "colour": colour,
            "stock": saved_stock,
            "minimum_stock": minimum_stock,
            "toner_id": toner_id,
            "created_at": now
        },
        "email_sent": email_result["sent"],
        "email_reason": email_result["reason"]
    }), 200

def table_exists(
    conn,
    table_name
):

    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        AND name = ?
        """,
        (table_name,)
    ).fetchone()

    return row is not None


def column_exists(
    conn,
    table_name,
    column_name
):

    if not table_exists(
        conn,
        table_name
    ):

        return False

    rows = conn.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    for row in rows:

        if row["name"] == column_name:

            return True

    return False


def add_column_if_missing(
    conn,
    table_name,
    column_name,
    definition
):

    if not column_exists(
        conn,
        table_name,
        column_name
    ):

        conn.execute(
            f"""
            ALTER TABLE {table_name}
            ADD COLUMN {column_name}
            {definition}
            """
        )


# =========================================================
# STOCK STATUS
# =========================================================

def get_stock_status(
    quantity,
    minimum_stock=5
):

    quantity = safe_int(
        quantity,
        0
    )

    minimum_stock = safe_int(
        minimum_stock,
        5
    )

    if minimum_stock < 1:

        minimum_stock = 5

    if quantity <= 0:

        return "Dead Stock"

    if quantity < 5:

        return "Danger"

    if quantity < 10:

        return "Understock"

    if quantity > minimum_stock * 5:

        return "Overstock"

    return "Available"


def toner_status_counts():

    conn = get_db_connection()

    try:

        result = {
            "Available": 0,
            "Understock": 0,
            "Danger": 0,
            "Dead Stock": 0,
            "Overstock": 0
        }

        if not table_exists(
            conn,
            "toners"
        ):

            return result

        rows = conn.execute(
            """
            SELECT
                quantity,
                minimum_stock
            FROM toners
            WHERE
                deleted_at IS NULL
                OR deleted_at = ''
            """
        ).fetchall()

        for row in rows:

            status = get_stock_status(
                row["quantity"],
                row["minimum_stock"]
            )

            result.setdefault(
                status,
                0
            )

            result[status] += 1

        return result

    finally:

        conn.close()


# =========================================================
# SETTINGS HELPERS
# =========================================================

def get_settings_row():

    conn = get_db_connection()

    try:

        row = conn.execute(
            """
            SELECT *
            FROM settings
            WHERE id = 1
            LIMIT 1
            """
        ).fetchone()

        return row

    finally:

        conn.close()


def save_settings_to_db(data):

    conn = get_db_connection()

    try:

        # Make sure older databases have the new security column.
        add_column_if_missing(
            conn,
            "settings",
            "smtp_security",
            "TEXT DEFAULT 'TLS'"
        )

        existing_row = conn.execute(
            """
            SELECT *
            FROM settings
            WHERE id = 1
            """
        ).fetchone()

        existing = existing_row is not None

        incoming_password = str(
            data.get(
                "smtp_password",
                ""
            )
        )

        # The Settings page intentionally sends no password when the
        # password field is left blank. Preserve the existing password
        # instead of replacing it with an empty string.
        if incoming_password.strip():
            smtp_password = incoming_password
        elif existing_row is not None:
            smtp_password = str(
                existing_row["smtp_password"]
                or ""
            )
        else:
            smtp_password = ""

        smtp_port = safe_int(
            data.get(
                "smtp_port",
                587
            ),
            587
        )

        requested_security = clean_text(
            data.get(
                "smtp_security",
                ""
            )
        ).upper()

        if requested_security not in (
            "NONE",
            "SSL",
            "TLS"
        ):
            requested_security = (
                "SSL"
                if smtp_port == 465
                else "TLS"
            )

        values = (
            data.get(
                "smtp_server",
                ""
            ),

            smtp_port,

            requested_security,

            data.get(
                "smtp_username",
                ""
            ),

            smtp_password,

            data.get(
                "sender_email",
                ""
            ),

            data.get(
                "receiver_email",
                ""
            ),

            data.get(
                "receiver_email_2",
                ""
            ),

            data.get(
                "low_stock_enabled",
                1
            ),

            data.get(
                "danger_stock_enabled",
                1
            ),

            data.get(
                "dead_stock_enabled",
                1
            ),

            data.get(
                "overstock_enabled",
                1
            ),

            data.get(
                "default_minimum_stock",
                5
            )
        )

        if existing:

            conn.execute(
                """
                UPDATE settings
                SET
                    smtp_server = ?,
                    smtp_port = ?,
                    smtp_security = ?,
                    smtp_username = ?,
                    smtp_password = ?,
                    sender_email = ?,
                    receiver_email = ?,
                    receiver_email_2 = ?,
                    low_stock_enabled = ?,
                    danger_stock_enabled = ?,
                    dead_stock_enabled = ?,
                    overstock_enabled = ?,
                    default_minimum_stock = ?
                WHERE id = 1
                """,
                values
            )

        else:

            conn.execute(
                """
                INSERT INTO settings
                (
                    id,
                    smtp_server,
                    smtp_port,
                    smtp_security,
                    smtp_username,
                    smtp_password,
                    sender_email,
                    receiver_email,
                    receiver_email_2,
                    low_stock_enabled,
                    danger_stock_enabled,
                    dead_stock_enabled,
                    overstock_enabled,
                    default_minimum_stock
                )
                VALUES
                (
                    1,
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?
                )
                """,
                values
            )

        conn.commit()

    except Exception:

        conn.rollback()

        raise

    finally:

        conn.close()


def get_email_config():

    row = get_settings_row()

    if row is None:

        return None

    smtp_port = safe_int(
        row["smtp_port"],
        587
    )

    # Older databases do not have smtp_security. For those databases,
    # keep the previous convention: port 465 means SSL, everything else
    # means STARTTLS/TLS.
    try:
        stored_security = clean_text(
            row["smtp_security"]
        ).upper()
    except (KeyError, IndexError):
        stored_security = ""

    smtp_security = stored_security
    if smtp_security not in (
        "NONE",
        "SSL",
        "TLS"
    ):
        smtp_security = (
            "SSL"
            if smtp_port == 465
            else "TLS"
        )

    return {

        "smtp_server":
            row["smtp_server"]
            or "",

        "smtp_port":
            smtp_port,

        "smtp_security":
            smtp_security,

        "smtp_username":
            row["smtp_username"]
            or "",

        "smtp_password":
            row["smtp_password"]
            or "",

        "sender_email":
            row["sender_email"]
            or row["smtp_username"]
            or "",

        "receiver_email":
            row["receiver_email"]
            or "",

        "receiver_email_2":
            row["receiver_email_2"]
            or ""
            if "receiver_email_2" in row.keys()
            else "",

        "understock_enabled":
            safe_int(
                row["low_stock_enabled"],
                1
            ),

        "low_stock_enabled":
            safe_int(
                row["low_stock_enabled"],
                1
            ),

        "danger_stock_enabled":
            safe_int(
                row["danger_stock_enabled"],
                1
            ),

        "dead_stock_enabled":
            safe_int(
                row["dead_stock_enabled"],
                1
            ),

        "overstock_enabled":
            safe_int(
                row["overstock_enabled"],
                1
            )
    }


# =========================================================
# EMAIL LOG
# =========================================================

def log_email_alert(
    toner_id,
    toner_model,
    alert_type,
    recipient,
    status,
    message
):

    conn = get_db_connection()

    try:

        conn.execute(
            """
            INSERT INTO email_alerts
            (
                toner_id,
                toner_model,
                alert_type,
                recipient,
                email_status,
                sent_date,
                message
            )
            VALUES
            (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                toner_id,
                toner_model,
                alert_type,
                recipient,
                status,
                current_datetime(),
                message
            )
        )

        conn.commit()

    except Exception:

        conn.rollback()

        raise

    finally:

        conn.close()


# =========================================================
# SMTP CONNECTION HELPER
# =========================================================

def send_email_message(
    email,
    config
):

    smtp_server = clean_text(
        config.get(
            "smtp_server",
            ""
        )
    )

    smtp_port = safe_int(
        config.get(
            "smtp_port",
            587
        ),
        587
    )

    smtp_security = clean_text(
        config.get(
            "smtp_security",
            ""
        )
    ).upper()

    if smtp_security not in (
        "NONE",
        "SSL",
        "TLS"
    ):
        smtp_security = (
            "SSL"
            if smtp_port == 465
            else "TLS"
        )

    smtp_username = clean_text(
        config.get(
            "smtp_username",
            ""
        )
    )

    smtp_password = str(
        config.get(
            "smtp_password",
            ""
        )
    )

    # Keep the existing message headers, but submit each recipient as a
    # separate SMTP envelope. This avoids multi-recipient relay issues while
    # leaving the email content and existing callers unchanged.
    recipients = []

    for header_name in (
        "To",
        "Cc",
        "Bcc"
    ):
        for header_value in email.get_all(
            header_name,
            []
        ):
            recipients.extend(
                [
                    value.strip()
                    for value in str(header_value).split(",")
                    if value.strip()
                ]
            )

    def send_to_recipients(server):

        if not recipients:

            server.send_message(email)
            return

        # Send one complete message per recipient.  Build a fresh
        # EmailMessage for each delivery so the visible To header also
        # contains only that recipient.  This avoids SMTP relay/server
        # issues when multiple recipients are supplied while preserving
        # the existing email body, subject and sender.
        for recipient in recipients:

            individual_email = EmailMessage()

            for header_name, header_value in email.items():
                if header_name.lower() == "to":
                    continue
                individual_email[header_name] = header_value

            individual_email["To"] = recipient
            individual_email.set_content(email.get_content())

            server.send_message(
                individual_email,
                from_addr=email.get("From"),
                to_addrs=[recipient]
            )

    if smtp_security == "SSL":

        # Direct TLS/SSL connection. Do NOT call STARTTLS on this
        # connection. This is the correct mode for SMTP servers that
        # expose implicit SSL/TLS (commonly port 465).
        with smtplib.SMTP_SSL(
            smtp_server,
            smtp_port,
            timeout=30,
            context=ssl.create_default_context()
        ) as server:

            server.ehlo()

            server.login(
                smtp_username,
                smtp_password
            )

            send_to_recipients(server)

        return

    if smtp_security == "NONE":

        # Plain SMTP. No SSL and no STARTTLS. This is required for
        # some internal SMTP relays on port 25 when STARTTLS
        # is not advertised.
        with smtplib.SMTP(
            smtp_server,
            smtp_port,
            timeout=30
        ) as server:

            server.ehlo()

            # Some internal port-25 SMTP relays allow mail without
            # SMTP AUTH.  Others advertise AUTH but reject LOGIN
            # with 535 even though plain SMTP delivery is allowed.
            # For NONE mode, try authentication when credentials are
            # supplied, but if the server explicitly rejects AUTH,
            # continue with unauthenticated SMTP delivery.  This keeps
            # NONE truly plain SMTP and avoids forcing STARTTLS/SSL.
            if smtp_username and smtp_password:
                try:
                    server.login(
                        smtp_username,
                        smtp_password
                    )
                except smtplib.SMTPAuthenticationError as auth_error:
                    # Clear the failed AUTH state, then allow a plain
                    # SMTP relay attempt.  If the relay requires auth,
                    # the subsequent send will return the server's
                    # proper relay/authentication error.
                    try:
                        server.rset()
                    except Exception:
                        pass
                    print(
                        "SMTP AUTH rejected in NONE mode; "
                        "trying plain SMTP delivery:",
                        auth_error
                    )

            send_to_recipients(server)

        return

    # STARTTLS/TLS mode. Do not attempt STARTTLS blindly: some SMTP
    # servers accept plain SMTP but do not advertise the STARTTLS
    # extension, which is the error reported by the previous code.
    with smtplib.SMTP(
        smtp_server,
        smtp_port,
        timeout=30
    ) as server:

        server.ehlo()

        if not server.has_extn("starttls"):
            raise smtplib.SMTPException(
                "The SMTP server does not support STARTTLS. "
                "Choose None for plain SMTP, SSL with the server's SSL port, "
                "or use a server/port that supports STARTTLS."
            )

        server.starttls(
            context=ssl.create_default_context()
        )

        server.ehlo()

        server.login(
            smtp_username,
            smtp_password
        )

        send_to_recipients(server)

# =========================================================
# AUTOMATIC TONER ALERT EMAIL
# =========================================================

def send_toner_alert(
    toner,
    previous_quantity
):

    if toner is None:

        return {
            "sent": False,
            "reason":
                "Toner record not found."
        }

    config = get_email_config()

    if config is None:

        return {
            "sent": False,
            "reason":
                "Email configuration not found."
        }

    old_quantity = safe_int(
        previous_quantity,
        0
    )

    new_quantity = safe_int(
        toner["quantity"],
        0
    )

    minimum_stock = safe_int(
        toner["minimum_stock"],
        5
    )

    old_status = get_stock_status(
        old_quantity,
        minimum_stock
    )

    new_status = get_stock_status(
        new_quantity,
        minimum_stock
    )

    if old_status == new_status:

        return {
            "sent": False,
            "reason":
                "Stock status did not change."
        }

    alert_settings = {

        "Understock": (
            safe_int(
                config.get(
                    "understock_enabled",
                    1
                ),
                1
            ),
            "UNDERSTOCK"
        ),

        "Danger": (
            safe_int(
                config.get(
                    "danger_stock_enabled",
                    1
                ),
                1
            ),
            "DANGER"
        ),

        "Dead Stock": (
            safe_int(
                config.get(
                    "dead_stock_enabled",
                    1
                ),
                1
            ),
            "DEAD STOCK"
        ),

        "Overstock": (
            safe_int(
                config.get(
                    "overstock_enabled",
                    1
                ),
                1
            ),
            "OVERSTOCK"
        )
    }

    if new_status not in alert_settings:

        return {
            "sent": False,
            "reason":
                "No alert required."
        }

    enabled, alert_type = (
        alert_settings[new_status]
    )

    if not enabled:

        return {
            "sent": False,
            "reason":
                f"{new_status} alert is disabled."
        }

    smtp_server = clean_text(
        config.get(
            "smtp_server",
            ""
        )
    )

    smtp_username = clean_text(
        config.get(
            "smtp_username",
            ""
        )
    )

    smtp_password = str(
        config.get(
            "smtp_password",
            ""
        )
    )

    receiver_email = clean_text(
        config.get(
            "receiver_email",
            ""
        )
    )

    receiver_email_2 = clean_text(
        config.get(
            "receiver_email_2",
            ""
        )
    )

    recipients = [receiver_email]

    if (
        receiver_email_2
        and receiver_email_2.lower()
        != receiver_email.lower()
    ):
        recipients.append(receiver_email_2)

    sender_email = clean_text(
        config.get(
            "sender_email",
            ""
        )
    )

    if not smtp_server:

        return {
            "sent": False,
            "reason":
                "SMTP server is not configured."
        }

    if not smtp_username:

        return {
            "sent": False,
            "reason":
                "SMTP username is not configured."
        }

    if not smtp_password:

        return {
            "sent": False,
            "reason":
                "SMTP password is not configured."
        }

    if not receiver_email:

        return {
            "sent": False,
            "reason":
                "Receiver email is not configured."
        }

    if not sender_email:

        sender_email = smtp_username

    asset_no = ""

    try:

        if "asset_no" in toner.keys():

            asset_no = (
                toner["asset_no"]
                or ""
            )

    except Exception:

        asset_no = ""

    if new_status == "Understock":

        subject = (
            "Smart Toner Management System - "
            "UNDERSTOCK ALERT"
        )

        body = f"""
Smart Toner Management System

Toner Stock Alert
=================

Alert Type   : UNDERSTOCK
Asset No     : {asset_no or "Not Specified"}
Toner Model  : {toner["toner_model"]}
Colour       : {toner["colour"] or "Not Specified"}
Previous Qty : {old_quantity}
Current Qty  : {new_quantity}
Minimum Qty  : {minimum_stock}
Status       : UNDERSTOCK

The toner stock has fallen below 10 units.

Please arrange toner replenishment.
""".strip()

    elif new_status == "Danger":

        subject = (
            "Smart Toner Management System - "
            "DANGER ALERT"
        )

        body = f"""
Smart Toner Management System

Emergency Toner Stock Alert
===========================

Alert Type   : DANGER
Asset No     : {asset_no or "Not Specified"}
Toner Model  : {toner["toner_model"]}
Colour       : {toner["colour"] or "Not Specified"}
Previous Qty : {old_quantity}
Current Qty  : {new_quantity}
Minimum Qty  : {minimum_stock}
Status       : DANGER

The toner stock has fallen below 5 units.

Immediate toner replenishment is recommended.
""".strip()

    elif new_status == "Dead Stock":

        subject = (
            "Smart Toner Management System - "
            "DEAD STOCK ALERT"
        )

        body = f"""
Smart Toner Management System

Dead Toner Stock Alert
======================

Alert Type   : DEAD STOCK
Asset No     : {asset_no or "Not Specified"}
Toner Model  : {toner["toner_model"]}
Colour       : {toner["colour"] or "Not Specified"}
Previous Qty : {old_quantity}
Current Qty  : {new_quantity}
Minimum Qty  : {minimum_stock}
Status       : DEAD STOCK

The toner stock has reached 0.

Immediate action is required.
""".strip()

    elif new_status == "Overstock":

        overstock_limit = (
            minimum_stock * 5
        )

        subject = (
            "Smart Toner Management System - "
            "OVERSTOCK ALERT"
        )

        body = f"""
Smart Toner Management System

Toner Overstock Alert
=====================

Alert Type      : OVERSTOCK
Asset No        : {asset_no or "Not Specified"}
Toner Model     : {toner["toner_model"]}
Colour          : {toner["colour"] or "Not Specified"}
Previous Qty    : {old_quantity}
Current Qty     : {new_quantity}
Minimum Qty     : {minimum_stock}
Overstock Limit : {overstock_limit}
Status          : OVERSTOCK

The toner stock is above the configured overstock limit.

Please review the inventory.
""".strip()

    else:

        return {
            "sent": False,
            "reason":
                "No alert template available."
        }

    email = EmailMessage()

    email["Subject"] = subject
    email["From"] = sender_email
    email["To"] = ", ".join(recipients)

    email.set_content(
        body
    )

    try:

        send_email_message(
            email,
            config
        )

        try:

            log_email_alert(
                toner["id"],
                toner["toner_model"],
                alert_type,
                ", ".join(recipients),
                "SENT",
                "Automatic stock alert sent successfully."
            )

        except Exception as log_error:

            print(
                "EMAIL LOG ERROR:",
                log_error
            )

        return {
            "sent": True,
            "reason":
                "Email alert sent successfully."
        }

    except Exception as error:

        error_message = str(
            error
        )

        try:

            log_email_alert(
                toner["id"],
                toner["toner_model"],
                alert_type,
                ", ".join(recipients),
                "FAILED",
                error_message
            )

        except Exception as log_error:

            print(
                "EMAIL LOG ERROR:",
                log_error
            )

        return {
            "sent": False,
            "reason":
                error_message
        }


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

def initialize_database():

    conn = get_db_connection()

    try:

        # -------------------------------------------------
        # LOCATIONS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS locations
            (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                location_name TEXT NOT NULL UNIQUE,
                location_code TEXT,
                active INTEGER DEFAULT 1,
                created_at TEXT
            )
            """
        )

        # -------------------------------------------------
        # DEPARTMENTS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS departments
            (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                department_name TEXT NOT NULL,
                department_code TEXT,
                location TEXT,
                responsible_person TEXT,
                remarks TEXT,
                created_at TEXT
            )
            """
        )

        # -------------------------------------------------
        # USERS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users
            (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                employee_code TEXT,
                email TEXT,
                department_id INTEGER,
                location_id INTEGER,
                active INTEGER DEFAULT 1,
                created_at TEXT
            )
            """
        )

        # -------------------------------------------------
        # TONER MODELS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS toner_models
            (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand TEXT,
                model_name TEXT NOT NULL,
                toner_type TEXT,
                colour TEXT,
                minimum_stock INTEGER DEFAULT 5,
                maximum_stock INTEGER DEFAULT 50,
                remarks TEXT,
                active INTEGER DEFAULT 1,
                created_at TEXT
            )
            """
        )

        # -------------------------------------------------
        # PRINTER ASSETS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS printer_assets
            (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_number TEXT,
                printer_model TEXT,
                asset_type TEXT,
                description TEXT,
                department_id INTEGER,
                location_id INTEGER,
                assigned_user_id INTEGER,
                status TEXT DEFAULT 'Active',
                created_at TEXT
            )
            """
        )

        # -------------------------------------------------
        # ASSETS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assets
            (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_name TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                device_type TEXT DEFAULT 'Printer',
                user TEXT,
                location TEXT,
                toner_cartridge TEXT,
                colour TEXT,
                stock INTEGER DEFAULT 0,
                minimum_stock INTEGER DEFAULT 5,
                created_at TEXT
            )
            """
        )

        add_column_if_missing(
            conn,
            "assets",
            "device_type",
            "TEXT DEFAULT 'Printer'"
        )

        add_column_if_missing(
            conn,
            "assets",
            "colour",
            "TEXT"
        )

        add_column_if_missing(
            conn,
            "assets",
            "stock",
            "INTEGER DEFAULT 0"
        )

        add_column_if_missing(
            conn,
            "assets",
            "minimum_stock",
            "INTEGER DEFAULT 5"
        )

        add_column_if_missing(
            conn,
            "assets",
            "created_at",
            "TEXT"
        )

        # -------------------------------------------------
        # TONERS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS toners
            (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                toner_model TEXT NOT NULL,
                colour TEXT,
                quantity INTEGER DEFAULT 0,
                printer TEXT,
                minimum_stock INTEGER DEFAULT 5,
                remarks TEXT,
                issued_quantity INTEGER DEFAULT 0,
                created_at TEXT,
                toner_model_id INTEGER,
                printer_asset_id INTEGER,
                department_id INTEGER,
                asset_id INTEGER,
                asset_no TEXT
            )
            """
        )

        add_column_if_missing(
            conn,
            "toners",
            "toner_model_id",
            "INTEGER"
        )

        add_column_if_missing(
            conn,
            "toners",
            "printer_asset_id",
            "INTEGER"
        )

        add_column_if_missing(
            conn,
            "toners",
            "department_id",
            "INTEGER"
        )

        add_column_if_missing(
            conn,
            "toners",
            "issued_quantity",
            "INTEGER DEFAULT 0"
        )

        add_column_if_missing(
            conn,
            "toners",
            "created_at",
            "TEXT"
        )

        add_column_if_missing(
            conn,
            "toners",
            "asset_id",
            "INTEGER"
        )

        add_column_if_missing(
            conn,
            "toners",
            "asset_no",
            "TEXT"
        )

        add_column_if_missing(
            conn,
            "toners",
            "deleted_at",
            "TEXT"
        )

        # -------------------------------------------------
        # SEPARATE REFILL TONERS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS refill_toners
            (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                refill_name TEXT NOT NULL,
                asset_no TEXT,
                device_type TEXT DEFAULT 'Printer',
                toner_cartridge TEXT,
                colour TEXT,
                location TEXT NOT NULL,
                stock INTEGER DEFAULT 0,
                minimum_stock INTEGER DEFAULT 5,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
            """
        )

        add_column_if_missing(
            conn,
            "refill_toners",
            "assigned_user",
            "TEXT"
        )

        add_column_if_missing(
            conn,
            "refill_toners",
            "asset_no",
            "TEXT"
        )

        add_column_if_missing(
            conn,
            "refill_toners",
            "device_type",
            "TEXT DEFAULT 'Printer'"
        )

        add_column_if_missing(
            conn,
            "refill_toners",
            "toner_cartridge",
            "TEXT"
        )

        add_column_if_missing(
            conn,
            "refill_toners",
            "colour",
            "TEXT"
        )

        # Keep a normal lookup index here instead of a UNIQUE index.
        # Existing databases may already contain duplicate refill rows,
        # and a UNIQUE index would prevent the application from starting
        # during migration. New rows are still protected by the duplicate
        # check in /api/add-refill-toner.
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_refill_toners_name_location
            ON refill_toners
            (refill_name, location)
            """
        )

        # -------------------------------------------------
        # SEPARATE REFILL TRANSACTIONS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS refill_transactions
            (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                refill_id INTEGER NOT NULL,
                refill_name TEXT NOT NULL,
                transaction_type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                previous_stock INTEGER DEFAULT 0,
                new_stock INTEGER DEFAULT 0,
                transaction_date TEXT NOT NULL,
                transaction_time TEXT NOT NULL,
                user_id INTEGER,
                remarks TEXT,
                FOREIGN KEY (refill_id)
                    REFERENCES refill_toners(id)
                    ON DELETE CASCADE
            )
            """
        )

        # -------------------------------------------------
        # TONER TRANSACTIONS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS
            toner_transactions
            (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                toner_id INTEGER,
                toner_model TEXT NOT NULL,
                transaction_type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                previous_quantity INTEGER DEFAULT 0,
                new_quantity INTEGER DEFAULT 0,
                transaction_date TEXT NOT NULL,
                transaction_time TEXT NOT NULL,
                department_id INTEGER,
                printer_asset_id INTEGER,
                user_id INTEGER,
                location_id INTEGER,
                remarks TEXT
            )
            """
        )

        # -------------------------------------------------
        # EMAIL ALERTS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_alerts
            (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                toner_id INTEGER,
                toner_model TEXT,
                alert_type TEXT,
                recipient TEXT,
                email_status TEXT,
                sent_date TEXT,
                message TEXT
            )
            """
        )

        # -------------------------------------------------
        # SETTINGS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings
            (
                id INTEGER PRIMARY KEY,
                smtp_server TEXT,
                smtp_port INTEGER DEFAULT 587,
                smtp_security TEXT DEFAULT 'TLS',
                smtp_username TEXT,
                smtp_password TEXT,
                sender_email TEXT,
                receiver_email TEXT,
                receiver_email_2 TEXT,
                low_stock_enabled INTEGER DEFAULT 1,
                danger_stock_enabled INTEGER DEFAULT 1,
                dead_stock_enabled INTEGER DEFAULT 1,
                overstock_enabled INTEGER DEFAULT 1,
                default_minimum_stock INTEGER DEFAULT 5
            )
            """
        )

        add_column_if_missing(
            conn,
            "settings",
            "low_stock_enabled",
            "INTEGER DEFAULT 1"
        )

        add_column_if_missing(
            conn,
            "settings",
            "danger_stock_enabled",
            "INTEGER DEFAULT 1"
        )

        add_column_if_missing(
            conn,
            "settings",
            "dead_stock_enabled",
            "INTEGER DEFAULT 1"
        )

        add_column_if_missing(
            conn,
            "settings",
            "overstock_enabled",
            "INTEGER DEFAULT 1"
        )

        add_column_if_missing(
            conn,
            "settings",
            "default_minimum_stock",
            "INTEGER DEFAULT 5"
        )

        add_column_if_missing(
            conn,
            "settings",
            "smtp_security",
            "TEXT DEFAULT 'TLS'"
        )

        add_column_if_missing(
            conn,
            "settings",
            "receiver_email_2",
            "TEXT"
        )

        # Backfill the new security field for existing records.
        conn.execute(
            """
            UPDATE settings
            SET smtp_security =
                CASE
                    WHEN smtp_port = 465 THEN 'SSL'
                    ELSE 'TLS'
                END
            WHERE smtp_security IS NULL
               OR TRIM(smtp_security) = ''
            """
        )

        # -------------------------------------------------
        # LOGIN USERS
        # -------------------------------------------------

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_users
            (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                full_name TEXT DEFAULT 'Administrator',
                role TEXT DEFAULT 'Administrator',
                active INTEGER DEFAULT 1,
                created_at TEXT
            )
            """
        )

        # -------------------------------------------------
        # OPTIONAL INITIAL ADMIN ACCOUNT
        # -------------------------------------------------
        # Configure these environment variables when creating a fresh database.
        # No default username or password is embedded in this source file.
        bootstrap_username = os.environ.get(
            "APP_ADMIN_USERNAME", ""
        ).strip()
        bootstrap_password = os.environ.get(
            "APP_ADMIN_PASSWORD", ""
        )
        bootstrap_full_name = (
            os.environ.get("APP_ADMIN_FULL_NAME", "Administrator").strip()
            or "Administrator"
        )

        if bootstrap_username and bootstrap_password:
            admin = conn.execute(
                "SELECT id FROM login_users WHERE username = ?",
                (bootstrap_username,)
            ).fetchone()

            if admin is None:
                conn.execute(
                    """
                    INSERT INTO login_users
                    (
                        username,
                        password_hash,
                        full_name,
                        role,
                        active,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bootstrap_username,
                        generate_password_hash(bootstrap_password),
                        bootstrap_full_name,
                        "Administrator",
                        1,
                        current_datetime()
                    )
                )

        # -------------------------------------------------
        # DEFAULT SETTINGS
        # -------------------------------------------------

        setting = conn.execute(
            """
            SELECT id
            FROM settings
            WHERE id = 1
            """
        ).fetchone()

        if setting is None:

            conn.execute(
                """
                INSERT INTO settings
                (
                    id,
                    smtp_server,
                    smtp_port,
                    smtp_username,
                    smtp_password,
                    sender_email,
                    receiver_email,
                    low_stock_enabled,
                    danger_stock_enabled,
                    dead_stock_enabled,
                    overstock_enabled,
                    default_minimum_stock
                )
                VALUES
                (
                    1,
                    '',
                    587,
                    '',
                    '',
                    '',
                    '',
                    1,
                    1,
                    1,
                    1,
                    5
                )
                """
            )

        conn.commit()

    except Exception:

        conn.rollback()

        raise

    finally:

        conn.close()


# =========================================================
# LOGIN PROTECTION
# =========================================================

@app.before_request
def require_login():

    public_endpoints = {
        "login",
        "logout",
        "forgot_password",
        "verify_otp",
        "reset_password",
        "static"
    }

    if request.endpoint in public_endpoints:

        return None

    if not session.get(
        "logged_in"
    ):

        return redirect(
            url_for("login")
        )

    return None


# =========================================================
# LOGIN
# =========================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if session.get(
        "logged_in"
    ):

        return redirect(
            url_for("dashboard")
        )

    error = None

    if request.method == "POST":

        username = clean_text(
            request.form.get(
                "username"
            )
        )

        password = request.form.get(
            "password",
            ""
        )

        if (
            not username
            or not password
        ):

            error = (
                "Username and password are required."
            )

        else:

            conn = get_db_connection()

            try:

                user = conn.execute(
                    """
                    SELECT *
                    FROM login_users
                    WHERE username = ?
                    AND active = 1
                    """,
                    (username,)
                ).fetchone()

            finally:

                conn.close()

            if user is None:

                error = (
                    "Invalid username or password."
                )

            elif not check_password_hash(
                user["password_hash"],
                password
            ):

                error = (
                    "Invalid username or password."
                )

            else:

                session.clear()

                session["logged_in"] = True

                session["login_user_id"] = (
                    user["id"]
                )

                session["username"] = (
                    user["username"]
                )

                session["full_name"] = (
                    user["full_name"]
                    or user["username"]
                )

                session["role"] = (
                    user["role"]
                    or "Administrator"
                )

                return redirect(
                    url_for("dashboard")
                )

    return render_template(
        "login.html",
        error=error
    )


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# =========================================================
# MAIN PAGES
# =========================================================

@app.route("/")
def dashboard():

    return render_template(
        "dashboard.html"
    )


# =========================================================
# NEW ISSUE / RETURN / REFILL PAGE
# =========================================================

@app.route("/issue-return-refill")
def issue_return_refill():
    return redirect(url_for("dashboard") + "#ops")


@app.route("/add-toner")
def add_toner_page():
    return redirect(url_for("dashboard") + "#assets")


@app.route("/settings")
def settings_page():
    return redirect(url_for("dashboard") + "#email")


@app.route("/toners")
def toners_page():
    return redirect(url_for("dashboard") + "#inventory")


@app.route("/reports")
def reports_page():
    return redirect(url_for("dashboard") + "#transactions")


@app.route(
    "/api/delete-toner/<int:toner_id>",
    methods=["DELETE"]
)
def delete_toner_api(toner_id):

    conn = None

    try:

        conn = get_db_connection()

        # -------------------------------------------------
        # FIND TONER
        # -------------------------------------------------

        toner = conn.execute(
            """
            SELECT
                id,
                toner_model,
                colour,
                quantity,
                minimum_stock,
                asset_id,
                asset_no
            FROM toners
            WHERE id = ?
            """,
            (toner_id,)
        ).fetchone()

        if toner is None:

            return jsonify({
                "success": False,
                "error": "Toner not found."
            }), 404

        # -------------------------------------------------
        # DELETE TONER TRANSACTIONS
        # -------------------------------------------------

        conn.execute(
            """
            DELETE FROM toner_transactions
            WHERE toner_id = ?
            """,
            (toner_id,)
        )

        # -------------------------------------------------
        # DELETE TONER
        # -------------------------------------------------

        conn.execute(
            """
            DELETE FROM toners
            WHERE id = ?
            """,
            (toner_id,)
        )

        # -------------------------------------------------
        # DELETE RELATED ASSET
        # -------------------------------------------------

        if toner["asset_id"]:

            conn.execute(
                """
                DELETE FROM assets
                WHERE id = ?
                """,
                (toner["asset_id"],)
            )

        # -------------------------------------------------
        # COMMIT
        # -------------------------------------------------

        conn.commit()

        return jsonify({

            "success": True,

            "message":
                "Toner deleted successfully.",

            "toner_id":
                toner_id

        }), 200

    except Exception as error:

        if conn is not None:
            conn.rollback()

        print(
            "DELETE TONER ERROR:",
            error
        )

        return jsonify({

            "success": False,

            "error":
                str(error)

        }), 500

    finally:

        if conn is not None:
            conn.close()


# =========================================================
# ADD TONER
# =========================================================

@app.route(
    "/api/add-toner",
    methods=["POST"]
)
def add_toner_api():

    conn = None
    created_toner = None

    try:

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        # -------------------------------------------------
        # FORM DATA
        # -------------------------------------------------

        asset_no = clean_text(
            data.get(
                "asset_no"
            )
        )

        device_type = clean_text(
            data.get(
                "device_type",
                "Printer"
            )
        )
        if device_type.strip().lower() not in ("printer", "scanner"):
            device_type = "Printer"
        else:
            device_type = device_type.strip().title()

        asset_description = clean_text(
            data.get(
                "asset_description"
            )
        )

        asset_user = clean_text(
            data.get(
                "user"
            )
        )

        asset_location = clean_text(
            data.get(
                "location"
            )
        )

        toner_cartridge = clean_text(
            data.get(
                "toner_cartridge"
            )
        )

        # Model/name is distinct from
        # the part classification (Toner, Cartridge, Drum, Ribbon, etc.).
        # Older requests did not send toner_model, so retain their behavior.
        toner_model = clean_text(
            data.get("toner_model") or data.get("model_name") or toner_cartridge
        )
        if toner_model and not toner_cartridge:
            toner_cartridge = "Toner"

        colour = clean_text(
            data.get(
                "colour"
            )
        )

        stock = safe_int(
            data.get(
                "stock",
                0
            ),
            0
        )

        minimum_stock = safe_int(
            data.get(
                "minimum_stock",
                5
            ),
            5
        )

        if minimum_stock < 1:

            minimum_stock = 5

        # -------------------------------------------------
        # VALIDATION
        # -------------------------------------------------

        if not asset_no:

            return jsonify({
                "success": False,
                "error":
                    "Asset No is required."
            }), 400

        if not asset_description:

            return jsonify({
                "success": False,
                "error":
                    "Asset Description is required."
            }), 400

        if not asset_user:

            return jsonify({
                "success": False,
                "error":
                    "User is required."
            }), 400

        if not asset_location:

            return jsonify({
                "success": False,
                "error":
                    "Location is required."
            }), 400

        # -------------------------------------------------
        # TONER / CARTRIDGE
        # -------------------------------------------------

        if not toner_cartridge:
            toner_cartridge = "Normal"
        else:
            toner_cartridge = toner_cartridge.strip()
            if not toner_cartridge:
                toner_cartridge = "Normal"

        # Backward compatibility: older forms only sent a toner type.
        if not toner_model:
            toner_model = toner_cartridge

        allowed_colours = {"black": "Black", "cyan": "Cyan", "magenta": "Magenta", "yellow": "Yellow", "normal": "Normal"}
        if colour:
            colour_key = colour.strip().lower()
            if colour_key not in allowed_colours:
                return jsonify({
                    "success": False,
                    "error": "Colour must be Black, Cyan, Magenta, Yellow, or Normal."
                }), 400
            colour = allowed_colours[colour_key]

        if stock < 0:

            return jsonify({
                "success": False,
                "error":
                    "Stock cannot be negative."
            }), 400

        # -------------------------------------------------
        # DATABASE
        # -------------------------------------------------

        conn = get_db_connection()
        ensure_master_user_location(conn, asset_user, asset_location)

        # Asset No is intentionally reusable across different colours and
        # toner types. Avoid accidentally saving the same combination twice.
        duplicate = conn.execute(
            """
            SELECT id FROM assets
            WHERE LOWER(TRIM(asset_name)) = LOWER(TRIM(?))
              AND LOWER(TRIM(COALESCE(toner_cartridge, ''))) = LOWER(TRIM(?))
              AND LOWER(TRIM(COALESCE(colour, ''))) = LOWER(TRIM(?))
            LIMIT 1
            """,
            (asset_no, toner_cartridge, colour)
        ).fetchone()
        if duplicate is not None:
            return jsonify({
                "success": False,
                "error": "This Asset No already has this Toner / Cartridge Type and Colour. Choose another colour or type."
            }), 409

        # -------------------------------------------------
        # CURRENT DATE/TIME
        # -------------------------------------------------

        now = current_datetime()

        # -------------------------------------------------
        # INSERT ASSET
        # -------------------------------------------------

        asset_cursor = conn.execute(
            """
            INSERT INTO assets
            (
                asset_name,
                asset_type,
                device_type,
                user,
                location,
                toner_cartridge,
                colour,
                stock,
                minimum_stock,
                created_at
            )
            VALUES
            (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                asset_no,
                asset_description,
                device_type,
                asset_user,
                asset_location,
                toner_cartridge,
                colour,
                stock,
                minimum_stock,
                now
            )
        )

        asset_id = (
            asset_cursor.lastrowid
        )

        # -------------------------------------------------
        # INSERT TONER
        # -------------------------------------------------

        toner_cursor = conn.execute(
            """
            INSERT INTO toners
            (
                toner_model,
                colour,
                quantity,
                printer,
                minimum_stock,
                remarks,
                issued_quantity,
                created_at,
                asset_id,
                asset_no,
                deleted_at
            )
            VALUES
            (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL
            )
            """,
            (
                toner_model,
                colour,
                stock,
                asset_description,
                minimum_stock,
                "Created from Add Toner",
                0,
                now,
                asset_id,
                asset_no
            )
        )

        toner_id = (
            toner_cursor.lastrowid
        )

        # -------------------------------------------------
        # INITIAL TRANSACTION
        # -------------------------------------------------

        conn.execute(
            """
            INSERT INTO toner_transactions
            (
                toner_id,
                toner_model,
                transaction_type,
                quantity,
                previous_quantity,
                new_quantity,
                transaction_date,
                transaction_time,
                department_id,
                printer_asset_id,
                user_id,
                location_id,
                remarks
            )
            VALUES
            (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                toner_id,
                toner_model,
                "ADD",
                stock,
                0,
                stock,
                now[:10],
                now[11:16],
                None,
                None,
                None,
                None,
                "Initial toner stock added"
            )
        )

        # -------------------------------------------------
        # COMMIT
        # -------------------------------------------------

        conn.commit()

        # -------------------------------------------------
        # GET CREATED RECORD
        # -------------------------------------------------

        created_toner = conn.execute(
            """
            SELECT
                t.*,
                a.asset_name AS asset_no
            FROM toners t
            LEFT JOIN assets a
                ON t.asset_id = a.id
            WHERE t.id = ?
            """,
            (
                toner_id,
            )
        ).fetchone()

    except Exception as error:

        if conn is not None:

            conn.rollback()

        print(
            "ADD TONER ERROR:",
            error
        )

        return jsonify({
            "success": False,
            "error":
                str(error)
        }), 500

    finally:

        if conn is not None:

            conn.close()

    # -----------------------------------------------------
    # AUTOMATIC EMAIL ALERT
    # -----------------------------------------------------

    email_result = {
        "sent": False,
        "reason":
            "No initial alert required."
    }

    if created_toner is not None:

        initial_status = get_stock_status(
            created_toner["quantity"],
            created_toner["minimum_stock"]
        )

        if safe_int(
            created_toner["quantity"],
            0
        ) == 0:

            email_result = send_toner_alert(
                created_toner,
                1
            )

        elif initial_status in (
            "Understock",
            "Danger",
            "Overstock"
        ):

            email_result = send_toner_alert(
                created_toner,
                10
            )

    # -----------------------------------------------------
    # SUCCESS RESPONSE
    # -----------------------------------------------------

    return jsonify({

        "success":
            True,

        "message":
            "Toner asset and stock added successfully.",

        "asset": {

            "id":
                asset_id,

            "asset_no":
                asset_no,

            "asset_description":
                asset_description,

            "device_type":
                device_type,

            "user":
                asset_user,

            "location":
                asset_location,

            "toner_cartridge":
                toner_cartridge,

            "colour":
                colour,

            "stock":
                stock,

            "minimum_stock":
                minimum_stock,

            "status":
                get_stock_status(
                    stock,
                    minimum_stock
                ),

            "toner_id":
                toner_id,

            "created_at":
                now
        },

        "toner": {

            "id":
                toner_id,

            "toner_model":
                toner_model,

            "colour":
                colour,

            "quantity":
                stock,

            "minimum_stock":
                minimum_stock,

            "status":
                get_stock_status(
                    stock,
                    minimum_stock
                ),

            "asset_id":
                asset_id,

            "asset_no":
                asset_no,

            "created_at":
                now
        },

        "email_sent":
            email_result["sent"],

        "email_reason":
            email_result["reason"]
    }), 201


# =========================================================
# ASSETS API
# =========================================================

@app.route(
    "/api/assets"
)
def assets_api():

    conn = get_db_connection()

    try:

        rows = conn.execute(
            """
            SELECT
                a.id,
                a.asset_name,
                a.asset_type,
                a.device_type,
                a.user,
                a.location,
                a.toner_cartridge,
                a.colour,
                a.stock,
                a.minimum_stock,
                a.created_at,

                (
                    SELECT t.id
                    FROM toners t
                    WHERE
                        t.asset_id = a.id
                        AND (
                            t.deleted_at IS NULL
                            OR t.deleted_at = ''
                        )
                    ORDER BY t.id DESC
                    LIMIT 1
                ) AS toner_id,

                (
                    SELECT t.toner_model
                    FROM toners t
                    WHERE
                        t.asset_id = a.id
                        AND (
                            t.deleted_at IS NULL
                            OR t.deleted_at = ''
                        )
                    ORDER BY t.id DESC
                    LIMIT 1
                ) AS toner_model

            FROM assets a

            ORDER BY a.id DESC
            """
        ).fetchall()

        assets = []

        for row in rows:

            stock = safe_int(
                row["stock"],
                0
            )

            minimum_stock = safe_int(
                row["minimum_stock"],
                5
            )

            assets.append({

                "id":
                    row["id"],

                "asset_no":
                    row["asset_name"]
                    or "",

                "asset_number":
                    row["asset_name"]
                    or "",

                "asset_name":
                    row["asset_name"]
                    or "",

                "asset_description":
                    row["asset_type"]
                    or "",

                "asset_type":
                    row["asset_type"]
                    or "",

                "device_type":
                    clean_text(row["device_type"])
                    or ("Scanner" if "scanner" in clean_text(row["asset_type"]).lower() else "Printer"),

                "user":
                    row["user"]
                    or "",

                "location":
                    row["location"]
                    or "",

                "toner_cartridge":
                    row["toner_cartridge"]
                    or "",

                "colour":
                    row["colour"]
                    or "",

                "color":
                    row["colour"]
                    or "",

                "stock":
                    stock,

                "minimum_stock":
                    minimum_stock,

                "status":
                    get_stock_status(
                        stock,
                        minimum_stock
                    ),

                "toner_id":
                    row["toner_id"],

                "toner_model":
                    row["toner_model"] or "",

                "created_at":
                    row["created_at"]
                    or ""
            })

        unique_asset_numbers = {
            clean_text(row["asset_name"]).casefold()
            for row in rows
            if clean_text(row["asset_name"])
        }

        return jsonify({

            "success":
                True,

            "assets":
                assets,

            "total_assets":
                len(unique_asset_numbers),

            "total_records":
                len(assets)
        })

    finally:

        conn.close()


# =========================================================
# ADD ASSET ONLY (E.G. SCANNERS WITHOUT TONER STOCK)
# =========================================================

@app.route("/api/assets/add", methods=["POST"])
def add_asset_only_api():
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        asset_no = clean_text(data.get("asset_no"))
        device_type = clean_text(data.get("device_type", data.get("asset_type", "Printer")))
        if device_type.strip().lower() not in ("printer", "scanner"):
            device_type = "Printer"
        else:
            device_type = device_type.strip().title()
        asset_description = clean_text(data.get("asset_description"))
        asset_user = clean_text(data.get("user"))
        asset_location = clean_text(data.get("location"))
        colour = clean_text(data.get("colour"))
        minimum_stock = safe_int(data.get("minimum_stock", 5), 5)
        if minimum_stock < 1:
            minimum_stock = 5
        if not asset_no:
            return jsonify({"success": False, "error": "Asset No is required."}), 400
        if not asset_description:
            return jsonify({"success": False, "error": "Asset Description is required."}), 400
        if not asset_user:
            return jsonify({"success": False, "error": "User is required."}), 400
        if not asset_location:
            return jsonify({"success": False, "error": "Location is required."}), 400

        conn = get_db_connection()
        ensure_master_user_location(conn, asset_user, asset_location)
        now = current_datetime()
        cur = conn.execute(
            """INSERT INTO assets
               (asset_name, asset_type, device_type, user, location, toner_cartridge, colour, stock, minimum_stock, created_at)
               VALUES (?, ?, ?, ?, ?, '', ?, 0, ?, ?)""",
            (asset_no, asset_description, device_type, asset_user, asset_location, colour, minimum_stock, now)
        )
        conn.commit()
        return jsonify({
            "success": True,
            "message": "Asset saved successfully. No toner stock record was created.",
            "asset": {
                "id": cur.lastrowid,
                "asset_no": asset_no,
                "asset_description": asset_description,
                "device_type": device_type,
                "user": asset_user,
                "location": asset_location,
                "toner_cartridge": "",
                "colour": colour,
                "stock": 0,
                "minimum_stock": minimum_stock,
                "created_at": now
            }
        }), 201
    except Exception as error:
        if conn is not None:
            conn.rollback()
        app.logger.exception("ADD ASSET ONLY ERROR")
        return jsonify({"success": False, "error": str(error)}), 500
    finally:
        if conn is not None:
            conn.close()


# =========================================================
# EDIT NORMAL TONER
# =========================================================
# The Add Toner page sends normal toner edits to /api/toners/edit.
# Update the selected toner record and its related asset record by ID.
# Asset No may be reused by any normal toner record.

@app.route(
    "/api/toners/edit",
    methods=["POST"]
)
def edit_toner_api():

    conn = None

    try:

        data = request.get_json(silent=True) or {}

        toner_id = safe_int(
            data.get("toner_id", data.get("id")),
            0
        )

        asset_no = clean_text(data.get("asset_no", ""))
        device_type = clean_text(data.get("device_type", data.get("asset_type", "Printer")))
        if device_type.strip().lower() not in ("printer", "scanner"):
            device_type = "Printer"
        else:
            device_type = device_type.strip().title()
        asset_description = clean_text(data.get("asset_description", ""))
        asset_user = clean_text(data.get("user", ""))
        asset_location = clean_text(data.get("location", ""))
        toner_cartridge = clean_text(data.get("toner_cartridge", ""))
        colour = clean_text(data.get("colour", ""))
        stock = safe_int(data.get("stock", 0), -1)
        minimum_stock = safe_int(data.get("minimum_stock", 5), 5)

        if toner_id <= 0:
            return jsonify({
                "success": False,
                "error": "Invalid toner ID."
            }), 400

        if not asset_no:
            return jsonify({
                "success": False,
                "error": "Asset No is required."
            }), 400

        if not asset_description:
            return jsonify({
                "success": False,
                "error": "Asset Description is required."
            }), 400

        if not asset_user:
            return jsonify({
                "success": False,
                "error": "User is required."
            }), 400

        if not asset_location:
            return jsonify({
                "success": False,
                "error": "Location is required."
            }), 400

        if not toner_cartridge:
            return jsonify({
                "success": False,
                "error": "Toner / Cartridge is required."
            }), 400

        if stock < 0:
            return jsonify({
                "success": False,
                "error": "Stock cannot be negative."
            }), 400

        if minimum_stock < 1:
            minimum_stock = 5

        conn = get_db_connection()
        ensure_master_user_location(conn, asset_user, asset_location)

        toner = conn.execute(
            """
            SELECT
                id,
                asset_id,
                asset_no,
                toner_model,
                quantity,
                minimum_stock
            FROM toners
            WHERE id = ?
              AND (deleted_at IS NULL OR deleted_at = '')
            LIMIT 1
            """,
            (toner_id,)
        ).fetchone()

        if toner is None:
            return jsonify({
                "success": False,
                "error": "Toner not found."
            }), 404

        asset_id = safe_int(toner["asset_id"], 0)

        if asset_id <= 0:
            return jsonify({
                "success": False,
                "error": "Related asset record not found."
            }), 404

        asset = conn.execute(
            """
            SELECT id
            FROM assets
            WHERE id = ?
            LIMIT 1
            """,
            (asset_id,)
        ).fetchone()

        if asset is None:
            return jsonify({
                "success": False,
                "error": "Related asset record not found."
            }), 404

        old_quantity = safe_int(toner["quantity"], 0)

        # No Asset No duplicate check here: the same Asset No can be
        # associated with multiple normal toner records.
        conn.execute(
            """
            UPDATE assets
            SET
                asset_name = ?,
                asset_type = ?,
                device_type = ?,
                user = ?,
                location = ?,
                toner_cartridge = ?,
                colour = ?,
                stock = ?,
                minimum_stock = ?
            WHERE id = ?
            """,
            (
                asset_no,
                asset_description,
                device_type,
                asset_user,
                asset_location,
                toner_cartridge,
                colour,
                stock,
                minimum_stock,
                asset_id
            )
        )

        conn.execute(
            """
            UPDATE toners
            SET
                toner_model = ?,
                colour = ?,
                quantity = ?,
                printer = ?,
                minimum_stock = ?,
                asset_no = ?
            WHERE id = ?
            """,
            (
                toner_cartridge,
                colour,
                stock,
                asset_description,
                minimum_stock,
                asset_no,
                toner_id
            )
        )

        conn.commit()

        updated = conn.execute(
            """
            SELECT
                t.id,
                t.toner_model,
                t.colour,
                t.quantity,
                t.minimum_stock,
                t.asset_id,
                t.asset_no,
                a.asset_name,
                a.asset_type,
                a.user,
                a.location
            FROM toners t
            LEFT JOIN assets a
                ON a.id = t.asset_id
            WHERE t.id = ?
            LIMIT 1
            """,
            (toner_id,)
        ).fetchone()

        return jsonify({
            "success": True,
            "message": "Toner updated successfully.",
            "toner": {
                "id": updated["id"],
                "toner_model": updated["toner_model"] or "",
                "colour": updated["colour"] or "",
                "quantity": safe_int(updated["quantity"], 0),
                "minimum_stock": safe_int(updated["minimum_stock"], 5),
                "asset_id": updated["asset_id"],
                "asset_no": updated["asset_no"] or updated["asset_name"] or "",
                "asset_description": updated["asset_type"] or "",
                "user": updated["user"] or "",
                "location": updated["location"] or "",
                "status": get_stock_status(
                    updated["quantity"],
                    updated["minimum_stock"]
                )
            },
            "old_quantity": old_quantity,
            "new_quantity": safe_int(updated["quantity"], 0)
        }), 200

    except Exception as error:

        if conn is not None:
            conn.rollback()

        print(
            "EDIT TONER ERROR:",
            error
        )

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500

    finally:

        if conn is not None:
            conn.close()


# =========================================================
# DELETE ASSET - SOFT DELETE
# =========================================================

@app.route(
    "/api/assets/delete",
    methods=["POST"]
)
def delete_asset():

    conn = None

    try:

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        asset_id = safe_int(
            data.get(
                "asset_id",
                data.get("id")
            ),
            0
        )

        if asset_id <= 0:

            return jsonify({
                "success": False,
                "error":
                    "Invalid asset ID."
            }), 400

        conn = get_db_connection()

        asset = conn.execute(
            """
            SELECT id
            FROM assets
            WHERE id = ?
            """,
            (asset_id,)
        ).fetchone()

        if asset is None:

            return jsonify({
                "success": False,
                "error":
                    "Asset not found."
            }), 404

        deleted_time = current_datetime()

        conn.execute(
            """
            UPDATE toners
            SET deleted_at = ?
            WHERE asset_id = ?
            AND (
                deleted_at IS NULL
                OR deleted_at = ''
            )
            """,
            (
                deleted_time,
                asset_id
            )
        )

        conn.execute(
            """
            DELETE FROM assets
            WHERE id = ?
            """,
            (asset_id,)
        )

        conn.commit()

        return jsonify({

            "success":
                True,

            "deleted":
                True,

            "asset_id":
                asset_id,

            "message":
                "Asset deleted successfully. Transaction history has been preserved."
        })

    except Exception as error:

        if conn is not None:

            conn.rollback()

        print(
            "DELETE ASSET ERROR:",
            error
        )

        return jsonify({

            "success":
                False,

            "error":
                str(error)

        }), 500

    finally:

        if conn is not None:

            conn.close()


# =========================================================
# DASHBOARD API
# =========================================================

@app.route(
    "/api/dashboard"
)
def dashboard_api():

    conn = get_db_connection()

    try:

        asset_rows = conn.execute(
            """
            SELECT
                a.id,
                a.asset_name,
                a.asset_type,
                a.device_type,
                a.user,
                a.location,
                a.toner_cartridge,
                a.colour,
                a.stock,
                a.minimum_stock,
                a.created_at,

                (
                    SELECT t.id
                    FROM toners t
                    WHERE
                        t.asset_id = a.id
                        AND (
                            t.deleted_at IS NULL
                            OR t.deleted_at = ''
                        )
                    ORDER BY t.id DESC
                    LIMIT 1
                ) AS toner_id

            FROM assets a

            ORDER BY a.id DESC
            """
        ).fetchall()

        assets = []

        for row in asset_rows:

            stock = safe_int(
                row["stock"],
                0
            )

            minimum_stock = safe_int(
                row["minimum_stock"],
                5
            )

            assets.append({

                "id":
                    row["id"],

                "asset_no":
                    row["asset_name"]
                    or "",

                "asset_number":
                    row["asset_name"]
                    or "",

                "asset_name":
                    row["asset_name"]
                    or "",

                "asset_description":
                    row["asset_type"]
                    or "",

                "asset_type":
                    row["asset_type"]
                    or "",

                "device_type":
                    clean_text(row["device_type"])
                    or ("Scanner" if "scanner" in clean_text(row["asset_type"]).lower() else "Printer"),

                "user":
                    row["user"]
                    or "",

                "location":
                    row["location"]
                    or "",

                "toner_cartridge":
                    row["toner_cartridge"]
                    or "",

                "colour":
                    row["colour"]
                    or "",

                "color":
                    row["colour"]
                    or "",

                "stock":
                    stock,

                "minimum_stock":
                    minimum_stock,

                "status":
                    get_stock_status(
                        stock,
                        minimum_stock
                    ),

                "toner_id":
                    row["toner_id"],

                "created_at":
                    row["created_at"]
                    or ""
            })

        unique_asset_numbers = {
            clean_text(row["asset_name"]).casefold()
            for row in asset_rows
            if clean_text(row["asset_name"])
        }

        unique_types = set()

        for row in asset_rows:

            value = clean_text(
                row["asset_type"]
            ).lower()

            if value:

                unique_types.add(
                    value
                )

        unique_users = set()

        user_rows = conn.execute(
            """
            SELECT full_name
            FROM users
            WHERE active = 1
            ORDER BY full_name
            """
        ).fetchall()

        for row in user_rows:

            value = clean_text(
                row["full_name"]
            ).lower()

            if value:

                unique_users.add(
                    value
                )

        unique_locations = set()

        location_rows = conn.execute(
            """
            SELECT location_name
            FROM locations
            WHERE active = 1
            ORDER BY location_name
            """
        ).fetchall()

        for row in location_rows:

            value = clean_text(
                row["location_name"]
            ).lower()

            if value:

                unique_locations.add(
                    value
                )

        toner_record_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM toners
            WHERE
                deleted_at IS NULL
                OR deleted_at = ''
            """
        ).fetchone()[0]

        toner_stock = conn.execute(
            """
            SELECT COALESCE(
                SUM(quantity),
                0
            )
            FROM toners
            WHERE
                deleted_at IS NULL
                OR deleted_at = ''
            """
        ).fetchone()[0]

        toner_rows = conn.execute(
            """
            SELECT
                quantity,
                minimum_stock
            FROM toners
            WHERE
                deleted_at IS NULL
                OR deleted_at = ''
            """
        ).fetchall()

        stock_status = {
            "Available": 0,
            "Understock": 0,
            "Danger": 0,
            "Dead Stock": 0,
            "Overstock": 0
        }

        for row in toner_rows:

            status = get_stock_status(
                row["quantity"],
                row["minimum_stock"]
            )

            stock_status.setdefault(
                status,
                0
            )

            stock_status[status] += 1

        total_issued = conn.execute(
            """
            SELECT COALESCE(SUM(quantity), 0)
            FROM toner_transactions
            WHERE UPPER(transaction_type) = 'ISSUE'
            """
        ).fetchone()[0]

        total_refill_issued = conn.execute(
            """
            SELECT COALESCE(SUM(quantity), 0)
            FROM refill_transactions
            WHERE UPPER(transaction_type) = 'ISSUE'
            """
        ).fetchone()[0]

        total_issued = safe_int(total_issued, 0) + safe_int(total_refill_issued, 0)

        # Current outstanding issued stock (not lifetime issue history).
        # Normal toner uses the maintained issued_quantity field.
        current_normal_issued = conn.execute(
            """
            SELECT COALESCE(SUM(issued_quantity), 0)
            FROM toners
            WHERE deleted_at IS NULL
               OR deleted_at = ''
            """
        ).fetchone()[0]

        # Refill inventory does not store issued_quantity directly, so calculate
        # the outstanding issued balance from ISSUE minus RETURN transactions.
        current_refill_issued = conn.execute(
            """
            SELECT COALESCE(
                SUM(
                    CASE
                        WHEN UPPER(transaction_type) = 'ISSUE' THEN quantity
                        WHEN UPPER(transaction_type) = 'RETURN' THEN -quantity
                        ELSE 0
                    END
                ),
                0
            )
            FROM refill_transactions
            """
        ).fetchone()[0]

        current_issued = max(
            0,
            safe_int(current_normal_issued, 0)
            + safe_int(current_refill_issued, 0)
        )

        total_refill_records = conn.execute(
            "SELECT COUNT(*) FROM refill_toners"
        ).fetchone()[0]

        total_refill_stock = conn.execute(
            "SELECT COALESCE(SUM(stock), 0) FROM refill_toners"
        ).fetchone()[0]

        return jsonify({

            "success":
                True,

            "total_assets":
                len(unique_asset_numbers),

            "types_of_assets":
                len(unique_types),

            "asset_types":
                len(unique_types),

            "total_toners":
                safe_int(
                    toner_stock,
                    0
                ),

            "total_issued":
        safe_int(
                    total_issued,
                    0
                ),

            "current_issued":
                safe_int(
                    current_issued,
                    0
                ),

            # Count real active master records. Do not count names that
            # merely appear in assets.user or assets.location.
            "total_users":
                len(user_rows),

            "total_locations":
                len(location_rows),

            "total_refill_records":
                safe_int(total_refill_records, 0),

            "total_refill_stock":
                safe_int(total_refill_stock, 0),

            "available_stock":
                safe_int(
                    toner_stock,
                    0
                ),

            "toner_record_count":
                toner_record_count,

            "assets":
                assets,

            "types":
                sorted(
                    list(unique_types)
                ),

            "users":
                sorted(
                    list(unique_users)
                ),

            "locations":
                sorted(
                    list(unique_locations)
                ),

            "stock_status":
                stock_status
        })

    finally:

        conn.close()


# =========================================================
# TONERS API
# =========================================================

@app.route(
    "/api/toners"
)
def toners_api():

    conn = get_db_connection()

    try:

        rows = conn.execute(
            """
            SELECT
                t.id,
                t.toner_model,
                t.colour,
                t.quantity,
                t.printer,
                t.minimum_stock,
                t.remarks,
                t.issued_quantity,
                t.created_at,
                t.toner_model_id,
                t.printer_asset_id,
                t.department_id,
                t.asset_id,
                t.asset_no,

                tm.brand,
                tm.model_name,
                tm.toner_type,
                tm.maximum_stock,

                pa.asset_number,
                pa.printer_model,

                d.department_name

            FROM toners t

            LEFT JOIN toner_models tm
                ON t.toner_model_id = tm.id

            LEFT JOIN printer_assets pa
                ON t.printer_asset_id = pa.id

            LEFT JOIN departments d
                ON t.department_id = d.id

            WHERE
                t.deleted_at IS NULL
                OR t.deleted_at = ''

            ORDER BY
                t.toner_model ASC,
                t.id DESC
            """
        ).fetchall()

        toners = []

        for row in rows:

            quantity = safe_int(
                row["quantity"],
                0
            )

            minimum_stock = safe_int(
                row["minimum_stock"],
                5
            )

            if (
                row["asset_number"]
                and row["printer_model"]
            ):

                printer_display = (
                    str(
                        row["asset_number"]
                    )
                    + " - "
                    + str(
                        row["printer_model"]
                    )
                )

            else:

                printer_display = (
                    row["printer"]
                    or row["asset_number"]
                    or row["printer_model"]
                    or "Not Mapped"
                )

            toners.append({

                "id":
                    row["id"],

                "toner_model":
                    row["toner_model"]
                    or "",

                "toner_model_id":
                    row["toner_model_id"],

                "brand":
                    row["brand"]
                    or "",

                "model_name":
                    row["model_name"]
                    or row["toner_model"]
                    or "",

                "toner_type":
                    row["toner_type"]
                    or "",

                "colour":
                    row["colour"]
                    or "Not Specified",

                "quantity":
                    quantity,

                "printer":
                    printer_display,

                "printer_asset_id":
                    row["printer_asset_id"],

                "asset_number":
                    row["asset_number"]
                    or "",

                "printer_model":
                    row["printer_model"]
                    or "",

                "asset_id":
                    row["asset_id"],

                "asset_no":
                    row["asset_no"]
                    or "",

                "department_id":
                    row["department_id"],

                "department_name":
                    row["department_name"]
                    or "Not Assigned",

                "minimum_stock":
                    minimum_stock,

                "maximum_stock":
                    safe_int(
                        row["maximum_stock"],
                        minimum_stock * 5
                    ),

                "remarks":
                    row["remarks"]
                    or "",

                "issued_quantity":
                    safe_int(
                        row["issued_quantity"],
                        0
                    ),

                "created_at":
                    row["created_at"]
                    or "",

                "status":
                    get_stock_status(
                        quantity,
                        minimum_stock
                    )
            })

        model_names = set()

        for toner in toners:

            model = clean_text(
                toner["toner_model"]
            ).lower()

            if model:

                model_names.add(
                    model
                )

        return jsonify({

            "success":
                True,

            "toners":
                toners,

            "total_models":
                len(model_names),

            "total_records":
                len(toners),

            "total_stock":
                sum(
                    item["quantity"]
                    for item in toners
                ),

            "total_toners":
                len(toners)
        })

    finally:

        conn.close()


# =========================================================
# REFILL DATA API
# =========================================================

@app.route(
    "/api/refill-toners",
    methods=["GET"]
)
def refill_toners_api():

    conn = get_db_connection()

    try:

        rows = conn.execute(
            """
            SELECT
                id,
                refill_name,
                asset_no,
                device_type,
                toner_cartridge,
                colour,
                location,
                stock,
                minimum_stock,
                assigned_user,
                created_at,
                updated_at
            FROM refill_toners
            ORDER BY id DESC
            """
        ).fetchall()

        refill_toners = []

        for row in rows:

            stock = safe_int(
                row["stock"],
                0
            )

            minimum_stock = safe_int(
                row["minimum_stock"],
                5
            )

            refill_toners.append({
                "id": row["id"],
                "toner_id": row["id"],
                "refill_type": row["refill_name"],
                "refill_name": row["refill_name"],
                "asset_no": clean_text(row["asset_no"]),
                "device_type": clean_text(row["device_type"]) or "Printer",
                "toner_model": clean_text(row["toner_cartridge"] or row["refill_name"]),
                "toner_cartridge": clean_text(row["toner_cartridge"]),
                "colour": clean_text(row["colour"]) or "Not Specified",
                "quantity": stock,
                "stock": stock,
                "minimum_stock": minimum_stock,
                "issued_quantity": 0,
                "asset_id": None,
                "user": clean_text(row["assigned_user"]),
                "assigned_user": clean_text(row["assigned_user"]),
                "location": clean_text(row["location"]),
                "asset_location": clean_text(row["location"]),
                "created_at": row["created_at"] or "",
                "updated_at": row["updated_at"] or "",
                "status": get_stock_status(
                    stock,
                    minimum_stock
                )
            })

        return jsonify({
            "success": True,
            "refill_toners": refill_toners,
            "total_refill_records": len(refill_toners),
            "total_refill_stock": sum(item["stock"] for item in refill_toners)
        })

    except Exception as error:

        print("REFILL DATA API ERROR:", error)

        return jsonify({
            "success": False,
            "error": str(error),
            "refill_toners": []
        }), 500

    finally:
        conn.close()


# =========================================================
# CLEAR ONLY THE CARTRIDGE TYPE FROM A SAVED REFILL RECORD
# =========================================================

@app.route(
    "/api/refill-record/clear-cartridge-type",
    methods=["POST"],
    strict_slashes=False
)
def clear_refill_cartridge_type_api():
    """Clear only toner_cartridge; preserve the refill record, stock and history."""
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        refill_id = safe_int(data.get("refill_id", data.get("id")), 0)
        if refill_id <= 0:
            return jsonify({"success": False, "error": "Invalid refill record ID."}), 400

        conn = get_db_connection()
        record = conn.execute(
            """
            SELECT id, refill_name, asset_no, toner_cartridge, colour, stock, location
            FROM refill_toners
            WHERE id = ?
            LIMIT 1
            """,
            (refill_id,)
        ).fetchone()

        if record is None:
            return jsonify({"success": False, "error": "Refill record not found."}), 404

        previous_type = clean_text(record["toner_cartridge"])
        if not previous_type:
            return jsonify({
                "success": True,
                "message": "This refill record already has no cartridge type.",
                "refill_id": refill_id,
                "cartridge_type_cleared": False
            }), 200

        # Update only this one field. Do not delete the record or its transactions.
        if column_exists(conn, "refill_toners", "updated_at"):
            conn.execute(
                "UPDATE refill_toners SET toner_cartridge = '', updated_at = ? WHERE id = ?",
                (current_datetime(), refill_id)
            )
        else:
            conn.execute(
                "UPDATE refill_toners SET toner_cartridge = '' WHERE id = ?",
                (refill_id,)
            )

        conn.commit()
        return jsonify({
            "success": True,
            "message": "Cartridge type cleared. The refill record, stock, location and transaction history were preserved.",
            "refill_id": refill_id,
            "asset_no": clean_text(record["asset_no"]),
            "previous_cartridge_type": previous_type,
            "cartridge_type_cleared": True
        }), 200

    except Exception as error:
        if conn is not None:
            conn.rollback()
        app.logger.exception("CLEAR REFILL CARTRIDGE TYPE ERROR")
        return jsonify({"success": False, "error": str(error)}), 500
    finally:
        if conn is not None:
            conn.close()


# =========================================================
# DELETE REFILL TONER
# =========================================================

def _delete_refill_toner(refill_id):

    conn = None

    try:

        refill_id = safe_int(refill_id, 0)

        if refill_id <= 0:
            return jsonify({
                "success": False,
                "error": "Invalid refill ID."
            }), 400

        conn = get_db_connection()

        refill = conn.execute(
            """
            SELECT
                id,
                refill_name,
                location,
                stock
            FROM refill_toners
            WHERE id = ?
            """,
            (refill_id,)
        ).fetchone()

        if refill is None:
            return jsonify({
                "success": False,
                "error": "Refill record not found."
            }), 404

        # Delete history first, then the main refill record.
        conn.execute(
            """
            DELETE FROM refill_transactions
            WHERE refill_id = ?
            """,
            (refill_id,)
        )

        cursor = conn.execute(
            """
            DELETE FROM refill_toners
            WHERE id = ?
            """,
            (refill_id,)
        )

        if cursor.rowcount != 1:
            conn.rollback()
            return jsonify({
                "success": False,
                "error": "Refill record could not be deleted."
            }), 409

        conn.commit()

        return jsonify({
            "success": True,
            "deleted": True,
            "message": "Refill record deleted successfully.",
            "refill_id": refill_id,
            "refill_name": refill["refill_name"] or "",
            "location": refill["location"] or ""
        }), 200

    except Exception as error:

        if conn is not None:
            conn.rollback()

        print("DELETE REFILL TONER ERROR:", error)

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500

    finally:

        if conn is not None:
            conn.close()


# =========================================================
# DEDICATED SAVED REFILL RECORD DELETE API
# =========================================================
# This is the single endpoint used by the Refill page Delete button.

@app.route(
    "/api/refill-record/delete",
    methods=["POST"],
    strict_slashes=False
)
def delete_saved_refill_record_api():

    data = request.get_json(silent=True) or {}

    refill_id = safe_int(
        data.get(
            "refill_id",
            data.get("id")
        ),
        0
    )

    return _delete_refill_toner(refill_id)


# =========================================================
# DELETE REFILL API ROUTES
# =========================================================
#
# The UI uses the JSON endpoint below.  Multiple compatible routes are kept
# so an older cached page or an older frontend URL cannot break deletion.

@app.route(
    "/api/delete-refill",
    methods=["POST", "DELETE"],
    strict_slashes=False
)
def delete_refill_root_api():

    # Accept either JSON {"refill_id": 123} or a query/path value.
    data = request.get_json(silent=True) or {}

    refill_id = safe_int(
        data.get("refill_id", data.get("id", request.args.get("refill_id"))),
        0
    )

    return _delete_refill_toner(refill_id)


@app.route(
    "/api/delete-refill-toner",
    methods=["POST", "DELETE"],
    strict_slashes=False
)
def delete_refill_toner_json_api():

    data = request.get_json(silent=True) or {}

    refill_id = safe_int(
        data.get("refill_id", data.get("id", request.args.get("refill_id"))),
        0
    )

    return _delete_refill_toner(refill_id)


@app.route(
    "/api/delete-refill/<int:refill_id>",
    methods=["POST", "DELETE"],
    strict_slashes=False
)
def delete_refill_compat_api(refill_id):
    return _delete_refill_toner(refill_id)


@app.route(
    "/api/refill-toners/<int:refill_id>",
    methods=["DELETE", "POST"],
    strict_slashes=False
)
def delete_refill_toner_api(refill_id):
    return _delete_refill_toner(refill_id)


# =========================================================
# ADD REFILL TONER
# =========================================================

@app.route(
    "/api/add-refill-toner",
    methods=["POST"]
)
@app.route(
    "/api/refill-toners/add",
    methods=["POST"]
)
def add_refill_toner_api():

    conn = None

    try:

        data = request.get_json(silent=True) or {}

        refill_name = clean_text(data.get("refill_name", ""))
        device_type = clean_text(data.get("device_type", "Printer"))
        asset_no = clean_text(data.get("asset_no", ""))
        assigned_user = clean_text(data.get("user", data.get("assigned_user", "")))
        toner_cartridge = clean_text(data.get("toner_cartridge", ""))
        colour = clean_text(data.get("colour", ""))
        location = clean_text(data.get("location", ""))
        stock = safe_int(data.get("stock", 0), -1)
        minimum_stock = safe_int(data.get("minimum_stock", 5), 5)

        if device_type.strip().lower() not in ("printer", "scanner"):
            device_type = "Printer"
        else:
            device_type = device_type.strip().title()

        if minimum_stock < 1:
            minimum_stock = 5

        if toner_cartridge:
            toner_cartridge = toner_cartridge.strip()
            if not toner_cartridge:
                toner_cartridge = "Normal"
        else:
            toner_cartridge = "Normal"

        allowed_colours = {"black": "Black", "cyan": "Cyan", "magenta": "Magenta", "yellow": "Yellow", "normal": "Normal"}
        if colour.strip():
            colour_key = colour.strip().lower()
            if colour_key not in allowed_colours:
                return jsonify({"success": False, "error": "Colour must be Black, Cyan, Magenta, Yellow, or Normal."}), 400
            colour = allowed_colours[colour_key]
        else:
            colour = ""

        # Refill names remain free-text; colour and type identify each
        # separate stock record for the same Asset No.
        if not refill_name:
            return jsonify({
                "success": False,
                "error": "Asset Description is required for Refill Toner."
            }), 400

        if not asset_no:
            return jsonify({
                "success": False,
                "error": "Asset No is required."
            }), 400

        if not location:
            return jsonify({
                "success": False,
                "error": "Location is required."
            }), 400

        if stock < 0:
            return jsonify({
                "success": False,
                "error": "Stock cannot be negative."
            }), 400

        conn = get_db_connection()
        ensure_master_user_location(conn, assigned_user, location)

        existing = conn.execute(
            """
            SELECT id
            FROM refill_toners
            WHERE LOWER(TRIM(COALESCE(asset_no, ''))) = LOWER(TRIM(?))
              AND LOWER(TRIM(COALESCE(toner_cartridge, ''))) = LOWER(TRIM(?))
              AND LOWER(TRIM(COALESCE(colour, ''))) = LOWER(TRIM(?))
            LIMIT 1
            """,
            (asset_no, toner_cartridge, colour)
        ).fetchone()

        if existing is not None:
            return jsonify({
                "success": False,
                "error": "This Asset No already has a refill record for this type and colour."
            }), 409

        now = current_datetime()

        cursor = conn.execute(
            """
            INSERT INTO refill_toners
            (refill_name, asset_no, assigned_user, device_type, toner_cartridge, colour, location, stock, minimum_stock, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (refill_name, asset_no, assigned_user, device_type, toner_cartridge, colour, location, stock, minimum_stock, now, now)
        )

        refill_id = cursor.lastrowid

        conn.execute(
            """
            INSERT INTO refill_transactions
            (refill_id, refill_name, transaction_type, quantity, previous_stock,
             new_stock, transaction_date, transaction_time, user_id, remarks)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                refill_id,
                refill_name,
                "ADD",
                stock,
                0,
                stock,
                now[:10],
                now[11:16],
                safe_int(
                    data.get("user_id", session.get("user_id")),
                    0
                ) or None,
                "Initial refill stock added"
            )
        )

        conn.commit()

        email_record = {
            "id": refill_id,
            "asset_no": asset_no,
            "toner_model": f"{refill_name} ({toner_cartridge})",
            "colour": colour,
            "quantity": stock,
            "minimum_stock": minimum_stock,
        }
        email_result = {"sent": False, "reason": "No initial alert required."}
        if stock == 0:
            email_result = send_toner_alert(email_record, 1)
        elif get_stock_status(stock, minimum_stock) in ("Understock", "Danger", "Dead Stock", "Overstock"):
            email_result = send_toner_alert(email_record, 10)

        return jsonify({
            "success": True,
            "message": "Refill data added successfully.",
            "email_sent": email_result["sent"],
            "email_reason": email_result["reason"],
            "refill": {
                "id": refill_id,
                "refill_name": refill_name,
                "asset_no": asset_no,
                "user": assigned_user,
                "assigned_user": assigned_user,
                "toner_cartridge": toner_cartridge,
                "colour": colour,
                "location": location,
                "stock": stock,
                "minimum_stock": minimum_stock,
                "status": get_stock_status(stock, minimum_stock),
                "created_at": now
            }
        }), 201

    except Exception as error:

        if conn is not None:
            conn.rollback()

        print("ADD REFILL TONER ERROR:", error)

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500

    finally:

        if conn is not None:
            conn.close()


# =========================================================
# EDIT REFILL TONER
# =========================================================

@app.route(
    "/api/edit-refill-toner",
    methods=["POST"]
)
def edit_refill_toner_api():

    conn = None

    try:

        data = request.get_json(silent=True) or {}

        refill_id = safe_int(data.get("refill_id", data.get("id")), 0)
        refill_name = clean_text(data.get("refill_name", ""))
        asset_no = clean_text(data.get("asset_no", ""))
        assigned_user = clean_text(data.get("user", data.get("assigned_user", "")))
        toner_cartridge = clean_text(data.get("toner_cartridge", ""))
        # Preserve the existing colour only when older clients omit the field.
        # The dashboard always sends the field, including an intentional blank
        # when the operator selects "Not Specified".
        colour_was_supplied = "colour" in data or "color" in data
        colour = clean_text(data.get("colour", data.get("color", "")))
        location = clean_text(data.get("location", ""))
        stock = safe_int(data.get("stock", 0), -1)
        minimum_stock = safe_int(data.get("minimum_stock", 5), 5)

        if refill_id <= 0:
            return jsonify({"success": False, "error": "Invalid refill ID."}), 400
        if not refill_name:
            return jsonify({"success": False, "error": "Asset Description is required for Refill Toner."}), 400
        if not asset_no:
            return jsonify({"success": False, "error": "Asset No is required."}), 400
        if not location:
            return jsonify({"success": False, "error": "Location is required."}), 400
        if stock < 0:
            return jsonify({"success": False, "error": "Stock cannot be negative."}), 400
        if minimum_stock < 1:
            minimum_stock = 5

        if toner_cartridge:
            toner_cartridge = toner_cartridge.strip()
        else:
            toner_cartridge = ""

        allowed_colours = {"black": "Black", "cyan": "Cyan", "magenta": "Magenta", "yellow": "Yellow", "normal": "Normal"}
        conn = get_db_connection()
        current = conn.execute(
            """
            SELECT id, asset_no, toner_cartridge, colour, location
            FROM refill_toners
            WHERE id = ?
            LIMIT 1
            """,
            (refill_id,)
        ).fetchone()
        if current is None:
            return jsonify({"success": False, "error": "Refill record not found."}), 404
        if not toner_cartridge:
            # Editing a legacy row with an empty part type must preserve its
            # existing empty value; silently converting it to "Normal" would
            # turn a description-only edit into an unintended identity change.
            toner_cartridge = clean_text(current["toner_cartridge"])
        if not colour_was_supplied:
            colour = clean_text(current["colour"]) or ""
        else:
            colour = colour.strip()
            if colour:
                colour_key = colour.lower()
                if colour_key not in allowed_colours:
                    return jsonify({"success": False, "error": "Colour must be Black, Cyan, Magenta, Yellow, or Normal."}), 400
                colour = allowed_colours[colour_key]
            else:
                # An empty value is an intentional edit, not a request to keep
                # the old colour.
                colour = ""
        ensure_master_user_location(conn, assigned_user, location)

        # IMPORTANT: legacy databases may already contain duplicate refill rows.
        # Editing the description/user/stock of one of those existing rows must
        # still work. Check for duplicates only when the inventory identity
        # (asset no, part type, colour, or location) is actually being changed.
        def _identity_text(value):
            normalized = clean_text(value).strip().casefold()
            # API output historically used "Not Specified" for a blank colour.
            # Treat both as the same identity so a simple description edit is
            # not mistaken for a colour change.
            return "" if normalized in {"", "not specified"} else normalized

        identity_fields_changed = any((
            _identity_text(current["asset_no"]) != _identity_text(asset_no),
            _identity_text(current["toner_cartridge"]) != _identity_text(toner_cartridge),
            _identity_text(current["colour"]) != _identity_text(colour),
            _identity_text(current["location"]) != _identity_text(location),
        ))

        if identity_fields_changed:
            duplicate = conn.execute(
                """
                SELECT id
                FROM refill_toners
                WHERE id <> ?
                  AND LOWER(TRIM(COALESCE(asset_no, ''))) = LOWER(TRIM(?))
                  AND LOWER(TRIM(COALESCE(toner_cartridge, ''))) = LOWER(TRIM(?))
                  AND LOWER(TRIM(COALESCE(colour, ''))) = LOWER(TRIM(?))
                  AND LOWER(TRIM(COALESCE(location, ''))) = LOWER(TRIM(?))
                LIMIT 1
                """,
                (refill_id, asset_no, toner_cartridge, colour, location)
            ).fetchone()

            if duplicate is not None:
                return jsonify({
                    "success": False,
                    "error": "Another refill record already exists for the selected Asset No, part type, colour, and location. Change those fields or edit the existing record instead."
                }), 409

        now = current_datetime()

        cursor = conn.execute(
            """
            UPDATE refill_toners
            SET
                refill_name = ?,
                asset_no = ?,
                assigned_user = ?,
                toner_cartridge = ?,
                colour = ?,
                location = ?,
                stock = ?,
                minimum_stock = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                refill_name,
                asset_no,
                assigned_user,
                toner_cartridge,
                colour,
                location,
                stock,
                minimum_stock,
                now,
                refill_id
            )
        )

        if cursor.rowcount != 1:
            conn.rollback()
            return jsonify({"success": False, "error": "Refill record not found."}), 404

        conn.commit()

        # Read the committed record back from the same database connection.
        # This confirms the exact saved text sent back to the dashboard.
        saved_row = conn.execute(
            """
            SELECT id, refill_name, asset_no, assigned_user, toner_cartridge,
                   colour, location, stock, minimum_stock, updated_at
            FROM refill_toners
            WHERE id = ?
            LIMIT 1
            """,
            (refill_id,)
        ).fetchone()
        if saved_row is None:
            return jsonify({"success": False, "error": "The refill record could not be read back after saving."}), 500

        saved_stock = safe_int(saved_row["stock"], 0)
        saved_minimum = safe_int(saved_row["minimum_stock"], 5)
        saved_user = clean_text(saved_row["assigned_user"])
        return jsonify({
            "success": True,
            "message": "Refill inventory updated and verified from the database.",
            "refill": {
                "id": safe_int(saved_row["id"], refill_id),
                "refill_name": clean_text(saved_row["refill_name"]),
                "asset_no": clean_text(saved_row["asset_no"]),
                "user": saved_user,
                "assigned_user": saved_user,
                "toner_cartridge": clean_text(saved_row["toner_cartridge"]),
                "colour": clean_text(saved_row["colour"]),
                "location": clean_text(saved_row["location"]),
                "asset_location": clean_text(saved_row["location"]),
                "stock": saved_stock,
                "quantity": saved_stock,
                "minimum_stock": saved_minimum,
                "status": get_stock_status(saved_stock, saved_minimum),
                "updated_at": saved_row["updated_at"] or ""
            }
        })

    except Exception as error:

        if conn is not None:
            conn.rollback()

        print("EDIT REFILL TONER ERROR:", error)

        return jsonify({"success": False, "error": str(error)}), 500

    finally:
        if conn is not None:
            conn.close()


# =========================================================
# SYNC ASSET STOCK
# =========================================================

def sync_asset_stock(
    conn,
    toner_id,
    new_quantity
):

    toner = conn.execute(
        """
        SELECT asset_id
        FROM toners
        WHERE id = ?
        """,
        (toner_id,)
    ).fetchone()

    if toner is None:

        return

    asset_id = toner["asset_id"]

    if asset_id:

        conn.execute(
            """
            UPDATE assets
            SET stock = ?
            WHERE id = ?
            """,
            (
                new_quantity,
                asset_id
            )
        )



# =========================================================
# SHARED ISSUE / RETURN FOR REFILL INVENTORY
# =========================================================
# The existing Issue / Return card can operate on either the normal
# `toners` table or the separate `refill_toners` inventory.


def process_refill_inventory_transaction(data, transaction_type):

    conn = None

    try:

        refill_id = safe_int(
            data.get("refill_id", data.get("toner_id")),
            0
        )

        quantity = safe_int(
            data.get("quantity"),
            0
        )

        transaction_date = (
            clean_text(data.get("transaction_date"))
            or datetime.now().strftime("%Y-%m-%d")
        )

        transaction_time = (
            clean_text(data.get("transaction_time"))
            or datetime.now().strftime("%H:%M")
        )

        user_id = safe_int(
            data.get("user_id", session.get("user_id")),
            0
        ) or None

        remarks = clean_text(data.get("remarks"))

        if refill_id <= 0:
            return jsonify({
                "success": False,
                "error": "Invalid refill ID."
            }), 400

        if quantity <= 0:
            return jsonify({
                "success": False,
                "error": f"{transaction_type.title()} quantity must be greater than 0."
            }), 400

        transaction_type = transaction_type.upper()
        if transaction_type not in ("ISSUE", "RETURN"):
            return jsonify({
                "success": False,
                "error": "Invalid refill transaction type."
            }), 400

        if transaction_type == "ISSUE" and user_id is None:
            return jsonify({
                "success": False,
                "error": "A registered user is required for toner issue."
            }), 400

        conn = get_db_connection()

        if transaction_type == "ISSUE":
            user = conn.execute(
                """
                SELECT id
                FROM users
                WHERE id = ?
                  AND active = 1
                LIMIT 1
                """,
                (user_id,)
            ).fetchone()

            if user is None:
                return jsonify({
                    "success": False,
                    "error": "Selected user is not an active registered user."
                }), 400

        refill = conn.execute(
            """
            SELECT
                id,
                refill_name,
                asset_no,
                toner_cartridge,
                colour,
                location,
                stock,
                minimum_stock,
                created_at,
                updated_at
            FROM refill_toners
            WHERE id = ?
            """,
            (refill_id,)
        ).fetchone()

        if refill is None:
            return jsonify({
                "success": False,
                "error": "Refill record not found."
            }), 404

        old_stock = max(0, safe_int(refill["stock"], 0))

        if transaction_type == "ISSUE":

            if quantity > old_stock:
                return jsonify({
                    "success": False,
                    "error": f"Only {old_stock} refill toner(s) are available."
                }), 400

            new_stock = old_stock - quantity
            default_remarks = "Refill toner issued"

        else:

            issued_balance = conn.execute(
                """
                SELECT COALESCE(
                    SUM(
                        CASE
                            WHEN UPPER(transaction_type) = 'ISSUE' THEN quantity
                            WHEN UPPER(transaction_type) = 'RETURN' THEN -quantity
                            ELSE 0
                        END
                    ),
                    0
                )
                FROM refill_transactions
                WHERE refill_id = ?
                """,
                (refill_id,)
            ).fetchone()[0]

            issued_balance = max(0, safe_int(issued_balance, 0))

            if quantity > issued_balance:
                return jsonify({
                    "success": False,
                    "error": f"Only {issued_balance} refill toner(s) are currently issued."
                }), 400

            new_stock = old_stock + quantity
            default_remarks = "Refill toner returned"

        now = current_datetime()

        cursor = conn.execute(
            """
            UPDATE refill_toners
            SET
                stock = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (new_stock, now, refill_id)
        )

        if cursor.rowcount != 1:
            conn.rollback()
            return jsonify({
                "success": False,
                "error": "Unable to update refill stock."
            }), 409

        conn.execute(
            """
            INSERT INTO refill_transactions
            (
                refill_id,
                refill_name,
                transaction_type,
                quantity,
                previous_stock,
                new_stock,
                transaction_date,
                transaction_time,
                user_id,
                remarks
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                refill_id,
                refill["refill_name"],
                transaction_type,
                quantity,
                old_stock,
                new_stock,
                transaction_date,
                transaction_time,
                user_id,
                remarks or default_remarks
            )
        )

        conn.commit()

        email_record = {
            "id": refill_id,
            "asset_no": refill["asset_no"] or "",
            "toner_model": f"{refill['refill_name']} ({refill['toner_cartridge'] or 'Not specified'})",
            "colour": refill["colour"] or "Not Specified",
            "quantity": new_stock,
            "minimum_stock": safe_int(refill["minimum_stock"], 5),
        }
        email_result = send_toner_alert(email_record, old_stock)

        return jsonify({
            "success": True,
            "message": (
                "Refill toner issued successfully."
                if transaction_type == "ISSUE"
                else "Refill toner returned successfully."
            ),
            "email_sent": email_result["sent"],
            "email_reason": email_result["reason"],
            "inventory_type": "refill",
            "transaction_type": transaction_type,
            "refill_id": refill_id,
            "asset_no": refill["asset_no"] or "",
            "refill_name": refill["refill_name"] or "",
            "toner_cartridge": refill["toner_cartridge"] or "",
            "colour": refill["colour"] or "Not Specified",
            "location": refill["location"] or "",
            "quantity": quantity,
            "previous_quantity": old_stock,
            "new_quantity": new_stock,
            "status": get_stock_status(
                new_stock,
                safe_int(refill["minimum_stock"], 5)
            )
        })

    except Exception as error:

        if conn is not None:
            conn.rollback()

        print(
            "REFILL ISSUE/RETURN ERROR:",
            error
        )

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500

    finally:

        if conn is not None:
            conn.close()


# =========================================================
# ISSUE TONER
# =========================================================

@app.route(
    "/api/toners/issue",
    methods=["POST"]
)
def issue_toner():

    conn = None

    try:

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        inventory_type = clean_text(
            data.get("inventory_type", "normal")
        ).lower()

        issue_user_id = safe_int(
            data.get("user_id"),
            0
        )

        if issue_user_id <= 0:
            return jsonify({
                "success": False,
                "error": "A registered user is required for toner issue."
            }), 400

        if inventory_type == "refill":
            return process_refill_inventory_transaction(
                data,
                "ISSUE"
            )

        toner_id = safe_int(
            data.get(
                "toner_id"
            ),
            0
        )

        quantity = safe_int(
            data.get(
                "quantity"
            ),
            0
        )

        transaction_date = clean_text(
            data.get(
                "transaction_date"
            )
        )

        transaction_time = clean_text(
            data.get(
                "transaction_time"
            )
        )

        remarks = clean_text(
            data.get(
                "remarks"
            )
        )

        if toner_id <= 0:

            return jsonify({
                "success": False,
                "error":
                    "Invalid toner ID."
            }), 400

        if quantity <= 0:

            return jsonify({
                "success": False,
                "error":
                    "Issue quantity must be greater than 0."
            }), 400

        if not transaction_date:

            transaction_date = datetime.now().strftime(
                "%Y-%m-%d"
            )

        if not transaction_time:

            transaction_time = datetime.now().strftime(
                "%H:%M"
            )

        conn = get_db_connection()

        user = conn.execute(
            """
            SELECT id
            FROM users
            WHERE id = ?
              AND active = 1
            LIMIT 1
            """,
            (issue_user_id,)
        ).fetchone()

        if user is None:
            return jsonify({
                "success": False,
                "error": "Selected user is not an active registered user."
            }), 400

        toner = conn.execute(
            """
            SELECT *
            FROM toners
            WHERE id = ?
            AND (
                deleted_at IS NULL
                OR deleted_at = ''
            )
            """,
            (toner_id,)
        ).fetchone()

        if toner is None:

            return jsonify({
                "success": False,
                "error":
                    "Active toner record not found."
            }), 404

        old_quantity = safe_int(
            toner["quantity"],
            0
        )

        old_issued = safe_int(
            toner["issued_quantity"],
            0
        )

        if quantity > old_quantity:

            return jsonify({
                "success": False,
                "error":
                    f"Only {old_quantity} toner(s) are available."
            }), 400

        new_quantity = (
            old_quantity - quantity
        )

        new_issued = (
            old_issued + quantity
        )

        conn.execute(
            """
            UPDATE toners
            SET
                quantity = ?,
                issued_quantity = ?
            WHERE id = ?
            """,
            (
                new_quantity,
                new_issued,
                toner_id
            )
        )

        sync_asset_stock(
            conn,
            toner_id,
            new_quantity
        )

        conn.execute(
            """
            INSERT INTO toner_transactions
            (
                toner_id,
                toner_model,
                transaction_type,
                quantity,
                previous_quantity,
                new_quantity,
                transaction_date,
                transaction_time,
                department_id,
                printer_asset_id,
                user_id,
                location_id,
                remarks
            )
            VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                toner_id,
                toner["toner_model"],
                "ISSUE",
                quantity,
                old_quantity,
                new_quantity,
                transaction_date,
                transaction_time,
                safe_int(
                    data.get(
                        "department_id"
                    ),
                    0
                ) or None,
                safe_int(
                    data.get(
                        "printer_asset_id"
                    ),
                    0
                ) or None,
                issue_user_id,
                safe_int(
                    data.get(
                        "location_id"
                    ),
                    0
                ) or None,
                remarks
                or "Toner issued"
            )
        )

        conn.commit()

        updated = conn.execute(
            """
            SELECT
                t.*,
                a.asset_name AS asset_no
            FROM toners t
            LEFT JOIN assets a
                ON t.asset_id = a.id
            WHERE t.id = ?
            """,
            (toner_id,)
        ).fetchone()

        minimum_stock = toner["minimum_stock"]

    except Exception as error:

        if conn is not None:

            conn.rollback()

        print(
            "ISSUE TONER ERROR:",
            error
        )

        return jsonify({
            "success": False,
            "error":
                str(error)
        }), 500

    finally:

        if conn is not None:

            conn.close()

    email_result = send_toner_alert(
        updated,
        old_quantity
    )

    return jsonify({

        "success":
            True,

        "message":
            "Toner issued successfully.",

        "toner_id":
            toner_id,

        "quantity_issued":
            quantity,

        "new_quantity":
            new_quantity,

        "issued_quantity":
            new_issued,

        "status":
            get_stock_status(
                new_quantity,
                minimum_stock
            ),

        "email_sent":
            email_result["sent"],

        "email_reason":
            email_result["reason"]
    })


# =========================================================
# RETURN TONER
# =========================================================

@app.route(
    "/api/toners/return",
    methods=["POST"]
)
def return_toner():

    conn = None

    try:

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        inventory_type = clean_text(
            data.get("inventory_type", "normal")
        ).lower()

        if inventory_type == "refill":
            return process_refill_inventory_transaction(
                data,
                "RETURN"
            )

        toner_id = safe_int(
            data.get(
                "toner_id"
            ),
            0
        )

        quantity = safe_int(
            data.get(
                "quantity"
            ),
            0
        )

        transaction_date = clean_text(
            data.get(
                "transaction_date"
            )
        )

        transaction_time = clean_text(
            data.get(
                "transaction_time"
            )
        )

        remarks = clean_text(
            data.get(
                "remarks"
            )
        )

        if toner_id <= 0:

            return jsonify({
                "success": False,
                "error":
                    "Invalid toner ID."
            }), 400

        if quantity <= 0:

            return jsonify({
                "success": False,
                "error":
                    "Return quantity must be greater than 0."
            }), 400

        if not transaction_date:

            transaction_date = datetime.now().strftime(
                "%Y-%m-%d"
            )

        if not transaction_time:

            transaction_time = datetime.now().strftime(
                "%H:%M"
            )

        conn = get_db_connection()

        toner = conn.execute(
            """
            SELECT *
            FROM toners
            WHERE id = ?
            AND (
                deleted_at IS NULL
                OR deleted_at = ''
            )
            """,
            (toner_id,)
        ).fetchone()

        if toner is None:

            return jsonify({
                "success": False,
                "error":
                    "Active toner record not found."
            }), 404

        old_quantity = safe_int(
            toner["quantity"],
            0
        )

        old_issued = safe_int(
            toner["issued_quantity"],
            0
        )

        if quantity > old_issued:

            return jsonify({
                "success": False,
                "error":
                    f"Only {old_issued} toner(s) are issued."
            }), 400

        new_quantity = (
            old_quantity + quantity
        )

        new_issued = (
            old_issued - quantity
        )

        conn.execute(
            """
            UPDATE toners
            SET
                quantity = ?,
                issued_quantity = ?
            WHERE id = ?
            """,
            (
                new_quantity,
                new_issued,
                toner_id
            )
        )

        sync_asset_stock(
            conn,
            toner_id,
            new_quantity
        )

        conn.execute(
            """
            INSERT INTO toner_transactions
            (
                toner_id,
                toner_model,
                transaction_type,
                quantity,
                previous_quantity,
                new_quantity,
                transaction_date,
                transaction_time,
                department_id,
                printer_asset_id,
                user_id,
                location_id,
                remarks
            )
            VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                toner_id,
                toner["toner_model"],
                "RETURN",
                quantity,
                old_quantity,
                new_quantity,
                transaction_date,
                transaction_time,
                safe_int(
                    data.get(
                        "department_id"
                    ),
                    0
                ) or None,
                safe_int(
                    data.get(
                        "printer_asset_id"
                    ),
                    0
                ) or None,
                safe_int(
                    data.get(
                        "user_id"
                    ),
                    0
                ) or None,
                safe_int(
                    data.get(
                        "location_id"
                    ),
                    0
                ) or None,
                remarks
                or "Toner returned"
            )
        )

        conn.commit()

        updated = conn.execute(
            """
            SELECT
                t.*,
                a.asset_name AS asset_no
            FROM toners t
            LEFT JOIN assets a
                ON t.asset_id = a.id
            WHERE t.id = ?
            """,
            (toner_id,)
        ).fetchone()

        minimum_stock = toner["minimum_stock"]

    except Exception as error:

        if conn is not None:

            conn.rollback()

        print(
            "RETURN TONER ERROR:",
            error
        )

        return jsonify({
            "success": False,
            "error":
                str(error)
        }), 500

    finally:

        if conn is not None:

            conn.close()

    email_result = send_toner_alert(
        updated,
        old_quantity
    )

    return jsonify({

        "success":
            True,

        "message":
            "Toner returned successfully.",

        "toner_id":
            toner_id,

        "quantity_returned":
            quantity,

        "new_quantity":
            new_quantity,

        "issued_quantity":
            new_issued,

        "status":
            get_stock_status(
                new_quantity,
                minimum_stock
            ),

        "email_sent":
            email_result["sent"],

        "email_reason":
            email_result["reason"]
    })


# =========================================================
# REFILL TONER
# =========================================================

@app.route(
    "/api/refill-toners/refill",
    methods=["POST"]
)
@app.route(
    "/api/toners/refill",
    methods=["POST"]
)
def refill_toner():

    conn = None

    try:

        data = request.get_json(silent=True) or {}

        # Frontend sends refill_id.  Keep toner_id as a compatibility fallback.
        refill_id = safe_int(
            data.get("refill_id", data.get("toner_id")),
            0
        )
        quantity = safe_int(
            data.get("quantity"),
            0
        )

        transaction_date = (
            clean_text(data.get("transaction_date"))
            or datetime.now().strftime("%Y-%m-%d")
        )
        transaction_time = (
            clean_text(data.get("transaction_time"))
            or datetime.now().strftime("%H:%M")
        )
        remarks = clean_text(data.get("remarks"))
        user_id = safe_int(
            data.get("user_id", session.get("user_id")),
            0
        ) or None

        if refill_id <= 0:
            return jsonify({
                "success": False,
                "error": "Invalid refill ID."
            }), 400

        if quantity <= 0:
            return jsonify({
                "success": False,
                "error": "Refill quantity must be greater than 0."
            }), 400

        conn = get_db_connection()

        refill = conn.execute(
            """
            SELECT
                id,
                refill_name,
                asset_no,
                toner_cartridge,
                colour,
                location,
                stock,
                minimum_stock
            FROM refill_toners
            WHERE id = ?
            """,
            (refill_id,)
        ).fetchone()

        if refill is None:
            return jsonify({
                "success": False,
                "error": "Refill record not found."
            }), 404

        old_stock = max(0, safe_int(refill["stock"], 0))
        now = current_datetime()

        # Atomic increment prevents two simultaneous refill requests from
        # overwriting each other's stock updates.
        cursor = conn.execute(
            """
            UPDATE refill_toners
            SET
                stock = stock + ?,
                updated_at = ?
            WHERE id = ?
            """,
            (quantity, now, refill_id)
        )

        if cursor.rowcount != 1:
            return jsonify({
                "success": False,
                "error": "Unable to update refill stock."
            }), 409

        updated = conn.execute(
            """
            SELECT
                id,
                refill_name,
                asset_no,
                toner_cartridge,
                colour,
                location,
                stock,
                minimum_stock,
                created_at,
                updated_at
            FROM refill_toners
            WHERE id = ?
            """,
            (refill_id,)
        ).fetchone()

        new_stock = safe_int(
            updated["stock"],
            old_stock + quantity
        )

        conn.execute(
            """
            INSERT INTO refill_transactions
            (
                refill_id,
                refill_name,
                transaction_type,
                quantity,
                previous_stock,
                new_stock,
                transaction_date,
                transaction_time,
                user_id,
                remarks
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                refill_id,
                updated["refill_name"],
                "REFILL",
                quantity,
                old_stock,
                new_stock,
                transaction_date,
                transaction_time,
                user_id,
                remarks or "Refill stock added"
            )
        )

        conn.commit()

        email_record = {
            "id": refill_id,
            "asset_no": updated["asset_no"] or "",
            "toner_model": f"{updated['refill_name']} ({updated['toner_cartridge'] or 'Not specified'})",
            "colour": updated["colour"] or "Not Specified",
            "quantity": new_stock,
            "minimum_stock": safe_int(updated["minimum_stock"], 5),
        }
        email_result = send_toner_alert(email_record, old_stock)

        return jsonify({
            "success": True,
            "message": "Refill stock updated successfully.",
            "email_sent": email_result["sent"],
            "email_reason": email_result["reason"],
            "refill_id": refill_id,
            "asset_no": updated["asset_no"] or "",
            "quantity_added": quantity,
            "previous_stock": old_stock,
            "new_stock": new_stock,
            "status": get_stock_status(
                new_stock,
                updated["minimum_stock"]
            ),
            "refill": {
                "id": updated["id"],
                "asset_no": updated["asset_no"] or "",
                "refill_name": updated["refill_name"],
                "toner_cartridge": updated["toner_cartridge"] or "",
                "colour": updated["colour"] or "Not Specified",
                "location": updated["location"],
                "stock": new_stock,
                "minimum_stock": safe_int(
                    updated["minimum_stock"],
                    5
                ),
                "created_at": updated["created_at"] or "",
                "updated_at": updated["updated_at"] or ""
            }
        })

    except Exception as error:

        if conn is not None:
            conn.rollback()

        print("REFILL TONER ERROR:", error)

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500

    finally:

        if conn is not None:
            conn.close()


# =========================================================
# DELETE TONER - SOFT DELETE
# =========================================================

@app.route(
    "/api/toners/delete",
    methods=["POST"]
)
def delete_toner():

    conn = None

    try:

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        toner_id = safe_int(
            data.get(
                "toner_id",
                data.get("id")
            ),
            0
        )

        if toner_id <= 0:

            return jsonify({
                "success": False,
                "error":
                    "Invalid toner ID."
            }), 400

        conn = get_db_connection()

        toner = conn.execute(
            """
            SELECT *
            FROM toners
            WHERE id = ?
            AND (
                deleted_at IS NULL
                OR deleted_at = ''
            )
            """,
            (toner_id,)
        ).fetchone()

        if toner is None:

            return jsonify({
                "success": False,
                "error":
                    "Active toner record not found."
            }), 404

        deleted_time = current_datetime()

        conn.execute(
            """
            UPDATE toners
            SET deleted_at = ?
            WHERE id = ?
            """,
            (
                deleted_time,
                toner_id
            )
        )

        asset_id = toner["asset_id"]

        if asset_id:

            conn.execute(
                """
                DELETE FROM assets
                WHERE id = ?
                """,
                (asset_id,)
            )

        conn.commit()

        return jsonify({

            "success":
                True,

            "deleted":
                True,

            "toner_id":
                toner_id,

            "asset_id":
                asset_id,

            "message":
                "Toner deleted successfully. Transaction history has been preserved."
        })

    except Exception as error:

        if conn is not None:

            conn.rollback()

        print(
            "DELETE TONER ERROR:",
            error
        )

        return jsonify({

            "success":
                False,

            "error":
                str(error)

        }), 500

    finally:

        if conn is not None:

            conn.close()


# =========================================================
# CURRENT ISSUED (OUTSTANDING) API
# =========================================================

@app.route(
    "/api/current-issued",
    methods=["GET"]
)
def current_issued_api():

    conn = get_db_connection()

    try:
        issued_records = []

        # -------------------------------------------------
        # NORMAL TONER
        # -------------------------------------------------
        # Build the outstanding quantity from ISSUE - RETURN
        # transactions when transaction history exists.  If a
        # toner has no transaction history, use the maintained
        # toners.issued_quantity field as the legacy fallback.
        toner_rows = conn.execute(
            """
            SELECT
                t.id,
                t.toner_model,
                t.colour,
                t.quantity,
                t.issued_quantity,
                t.asset_no,
                t.created_at
            FROM toners t
            WHERE t.deleted_at IS NULL
               OR t.deleted_at = ''
            ORDER BY t.id DESC
            """
        ).fetchall()

        toner_transactions = conn.execute(
            """
            SELECT
                tt.id,
                tt.toner_id,
                tt.transaction_type,
                tt.quantity,
                tt.transaction_date,
                tt.transaction_time,
                tt.remarks,
                pa.asset_number,
                u.full_name AS user_name,
                l.location_name
            FROM toner_transactions tt
            LEFT JOIN printer_assets pa
                ON tt.printer_asset_id = pa.id
            LEFT JOIN users u
                ON tt.user_id = u.id
            LEFT JOIN locations l
                ON tt.location_id = l.id
            ORDER BY
                tt.transaction_date DESC,
                tt.transaction_time DESC,
                tt.id DESC
            """
        ).fetchall()

        normal_balance = {}
        normal_has_history = set()
        latest_normal_issue = {}

        for row in toner_transactions:
            toner_id = row["toner_id"]
            if toner_id is None:
                continue

            toner_key = str(toner_id)
            transaction_type = str(
                row["transaction_type"] or ""
            ).strip().upper()
            quantity = max(0, safe_int(row["quantity"], 0))

            if transaction_type not in ("ISSUE", "RETURN"):
                continue

            normal_has_history.add(toner_key)
            current = normal_balance.get(toner_key, 0)

            if transaction_type == "ISSUE":
                normal_balance[toner_key] = current + quantity
                if toner_key not in latest_normal_issue:
                    latest_normal_issue[toner_key] = row
            else:
                normal_balance[toner_key] = max(0, current - quantity)

        for toner in toner_rows:
            toner_key = str(toner["id"])

            if toner_key in normal_has_history:
                current_issued = max(
                    0,
                    safe_int(normal_balance.get(toner_key, 0), 0)
                )
            else:
                current_issued = max(
                    0,
                    safe_int(toner["issued_quantity"], 0)
                )

            if current_issued <= 0:
                continue

            latest = latest_normal_issue.get(toner_key)
            asset_no = clean_text(toner["asset_no"])
            user_name = ""
            location_name = ""
            transaction_date = clean_text(toner["created_at"])
            transaction_time = ""
            remarks = "Currently issued"

            if latest is not None:
                asset_no = clean_text(
                    latest["asset_number"]
                ) or asset_no
                user_name = clean_text(
                    latest["user_name"]
                )
                location_name = clean_text(
                    latest["location_name"]
                )
                transaction_date = clean_text(
                    latest["transaction_date"]
                ) or transaction_date
                transaction_time = clean_text(
                    latest["transaction_time"]
                )
                remarks = clean_text(
                    latest["remarks"]
                ) or remarks

            issued_records.append({
                "id": "current-normal-" + toner_key,
                "transaction_key": "current-normal-" + toner_key,
                "inventory_type": "normal",
                "toner_id": toner["id"],
                "refill_id": None,
                "toner_model": clean_text(toner["toner_model"]) or "Unknown",
                "colour": clean_text(toner["colour"]),
                "transaction_type": "ISSUE",
                "quantity": current_issued,
                "transaction_date": transaction_date,
                "transaction_time": transaction_time,
                "asset_no": asset_no,
                "asset_number": asset_no,
                "user_name": user_name,
                "location_name": location_name,
                "remarks": remarks
            })

        # -------------------------------------------------
        # REFILL TONER / CARTRIDGE
        # -------------------------------------------------
        refill_rows = conn.execute(
            """
            SELECT
                rt.id,
                rt.refill_id,
                rt.transaction_type,
                rt.quantity,
                rt.transaction_date,
                rt.transaction_time,
                rt.remarks,
                rf.refill_name,
                rf.toner_cartridge,
                rf.asset_no,
                rf.location,
                u.full_name AS user_name
            FROM refill_transactions rt
            LEFT JOIN refill_toners rf
                ON rt.refill_id = rf.id
            LEFT JOIN users u
                ON rt.user_id = u.id
            ORDER BY
                rt.transaction_date DESC,
                rt.transaction_time DESC,
                rt.id DESC
            """
        ).fetchall()

        refill_balance = {}
        latest_refill_issue = {}

        for row in refill_rows:
            refill_id = row["refill_id"]
            if refill_id is None:
                continue

            refill_key = str(refill_id)
            transaction_type = str(
                row["transaction_type"] or ""
            ).strip().upper()
            quantity = max(0, safe_int(row["quantity"], 0))

            if transaction_type == "ISSUE":
                refill_balance[refill_key] = (
                    refill_balance.get(refill_key, 0) + quantity
                )
                if refill_key not in latest_refill_issue:
                    latest_refill_issue[refill_key] = row
            elif transaction_type == "RETURN":
                refill_balance[refill_key] = max(
                    0,
                    refill_balance.get(refill_key, 0) - quantity
                )

        for refill_key, current_issued in refill_balance.items():
            current_issued = max(0, safe_int(current_issued, 0))
            if current_issued <= 0:
                continue

            latest = latest_refill_issue.get(refill_key)
            if latest is None:
                continue

            issued_records.append({
                "id": "current-refill-" + refill_key,
                "transaction_key": "current-refill-" + refill_key,
                "inventory_type": "refill",
                "toner_id": None,
                "refill_id": latest["refill_id"],
                "toner_model": clean_text(latest["toner_cartridge"]) or "Unknown",
                "toner_cartridge": clean_text(latest["toner_cartridge"]),
                "refill_name": clean_text(latest["refill_name"]),
                "transaction_type": "ISSUE",
                "quantity": current_issued,
                "transaction_date": clean_text(latest["transaction_date"]),
                "transaction_time": clean_text(latest["transaction_time"]),
                "asset_no": clean_text(latest["asset_no"]),
                "asset_number": clean_text(latest["asset_no"]),
                "user_name": clean_text(latest["user_name"]),
                "location_name": clean_text(latest["location"]),
                "remarks": clean_text(latest["remarks"]) or "Currently issued"
            })

        issued_records.sort(
            key=lambda item: (
                item.get("transaction_date", ""),
                item.get("transaction_time", "")
            ),
            reverse=True
        )

        total_current_issued = sum(
            max(0, safe_int(item.get("quantity"), 0))
            for item in issued_records
        )

        return jsonify({
            "success": True,
            "current_issued": total_current_issued,
            "total_current_issued": total_current_issued,
            "records": issued_records,
            "transactions": issued_records
        })

    except Exception as error:
        print("CURRENT ISSUED API ERROR:", error)
        return jsonify({
            "success": False,
            "error": str(error),
            "current_issued": 0,
            "total_current_issued": 0,
            "records": []
        }), 500

    finally:
        conn.close()


# =========================================================
# TRANSACTION HISTORY
# =========================================================

@app.route(
    "/api/toner-transactions"
)
def toner_transactions_api():

    conn = get_db_connection()

    try:

        rows = conn.execute(
            """
            SELECT
                CAST(tt.id AS TEXT) AS transaction_key,
                tt.id AS id,
                tt.toner_id,
                COALESCE(t.toner_model, tt.toner_model) AS toner_model,
                tt.transaction_type,
                tt.quantity,
                tt.previous_quantity,
                tt.new_quantity,
                tt.transaction_date,
                tt.transaction_time,
                tt.department_id,
                d.department_name,
                tt.printer_asset_id,
                COALESCE(
                    NULLIF(TRIM(a.asset_name), ''),
                    NULLIF(TRIM(t.asset_no), ''),
                    NULLIF(TRIM(pa.asset_number), ''),
                    ''
                ) AS asset_number,
                pa.printer_model,
                tt.user_id,
                u.full_name AS user_name,
                tt.location_id,
                COALESCE(
                    NULLIF(TRIM(a.location), ''),
                    NULLIF(TRIM((
                        SELECT aa.location
                        FROM assets aa
                        WHERE LOWER(TRIM(aa.asset_name)) = LOWER(TRIM(
                            COALESCE(NULLIF(t.asset_no, ''), pa.asset_number, '')
                        ))
                        ORDER BY aa.id
                        LIMIT 1
                    )), ''),
                    NULLIF(TRIM(l.location_name), ''),
                    ''
                ) AS location_name,
                tt.remarks,
                'normal' AS inventory_type
            FROM toner_transactions tt
            LEFT JOIN toners t ON tt.toner_id = t.id
            LEFT JOIN assets a ON a.id = t.asset_id
            LEFT JOIN departments d ON tt.department_id = d.id
            LEFT JOIN printer_assets pa ON tt.printer_asset_id = pa.id
            LEFT JOIN users u ON tt.user_id = u.id
            LEFT JOIN locations l ON tt.location_id = l.id

            UNION ALL

            SELECT
                'refill-' || CAST(rt.id AS TEXT) AS transaction_key,
                rt.id AS id,
                NULL AS toner_id,
                COALESCE(rf.toner_cartridge, rt.refill_name) AS toner_model,
                rt.transaction_type,
                rt.quantity,
                rt.previous_stock AS previous_quantity,
                rt.new_stock AS new_quantity,
                rt.transaction_date,
                rt.transaction_time,
                NULL AS department_id,
                '' AS department_name,
                NULL AS printer_asset_id,
                COALESCE(rf.asset_no, '') AS asset_number,
                '' AS printer_model,
                rt.user_id,
                u.full_name AS user_name,
                NULL AS location_id,
                rf.location AS location_name,
                rt.remarks,
                'refill' AS inventory_type
            FROM refill_transactions rt
            LEFT JOIN refill_toners rf ON rt.refill_id = rf.id
            LEFT JOIN users u ON rt.user_id = u.id

            ORDER BY
                transaction_date DESC,
                transaction_time DESC,
                id DESC
            """
        ).fetchall()

        transactions = []

        for row in rows:

            transactions.append({

                "id":
                    row["id"],

                "transaction_key":
                    row["transaction_key"],

                "inventory_type":
                    row["inventory_type"] or "normal",

                "toner_id":
                    row["toner_id"],

                "toner_model":
                    row["toner_model"]
                    or "Unknown",

                "transaction_type":
                    str(
                        row["transaction_type"]
                        or ""
                    ).upper(),

                "quantity":
                    safe_int(
                        row["quantity"],
                        0
                    ),

                "previous_quantity":
                    safe_int(
                        row["previous_quantity"],
                        0
                    ),

                "new_quantity":
                    safe_int(
                        row["new_quantity"],
                        0
                    ),

                "transaction_date":
                    row["transaction_date"]
                    or "",

                "transaction_time":
                    row["transaction_time"]
                    or "",

                "department_id":
                    row["department_id"],

                "department_name":
                    row["department_name"]
                    or "",

                "printer_asset_id":
                    row["printer_asset_id"],

                "asset_number":
                    row["asset_number"]
                    or "",

                "printer_model":
                    row["printer_model"]
                    or "",

                "user_id":
                    row["user_id"],

                "user_name":
                    row["user_name"]
                    or "",

                "location_id":
                    row["location_id"],

                "location_name":
                    row["location_name"]
                    or "",

                "remarks":
                    row["remarks"]
                    or ""
            })

        return jsonify({

            "success":
                True,

            "transactions":
                transactions,

            "total_transactions":
                len(transactions)
        })

    finally:

        conn.close()


# =========================================================
# REPORTS API
# =========================================================

@app.route(
    "/api/reports"
)
def reports_api():

    period_type = clean_text(
        request.args.get(
            "type",
            "all"
        )
    )

    value = clean_text(
        request.args.get(
            "value",
            ""
        )
    )

    conn = get_db_connection()

    try:

        query = """
            SELECT
                id,
                toner_id,
                toner_model,
                transaction_type,
                quantity,
                previous_quantity,
                new_quantity,
                transaction_date,
                transaction_time,
                department_id,
                printer_asset_id,
                user_id,
                location_id,
                remarks
            FROM toner_transactions
        """

        params = []

        if (
            period_type == "date"
            and value
        ):

            query += """
                WHERE DATE(
                    transaction_date
                ) = ?
            """

            params.append(
                value
            )

        elif (
            period_type == "month"
            and value
        ):

            query += """
                WHERE strftime(
                    '%Y-%m',
                    transaction_date
                ) = ?
            """

            params.append(
                value
            )

        elif (
            period_type == "year"
            and value
        ):

            query += """
                WHERE strftime(
                    '%Y',
                    transaction_date
                ) = ?
            """

            params.append(
                value
            )

        query += """
            ORDER BY
                transaction_date DESC,
                id DESC
        """

        rows = conn.execute(
            query,
            params
        ).fetchall()

        transactions = []

        total_added = 0
        total_issued = 0
        total_returned = 0
        total_refilled = 0

        for row in rows:

            quantity = safe_int(
                row["quantity"],
                0
            )

            transaction_type = (
                row["transaction_type"]
                or ""
            ).upper()

            if transaction_type == "ADD":

                total_added += quantity

            elif transaction_type == "ISSUE":

                total_issued += quantity

            elif transaction_type == "RETURN":

                total_returned += quantity

            elif transaction_type == "REFILL":

                total_refilled += quantity

            transactions.append({

                "id":
                    row["id"],

                "toner_id":
                    row["toner_id"],

                "toner_model":
                    row["toner_model"]
                    or "Unknown",

                "transaction_type":
                    transaction_type,

                "quantity":
                    quantity,

                "previous_quantity":
                    safe_int(
                        row["previous_quantity"],
                        0
                    ),

                "new_quantity":
                    safe_int(
                        row["new_quantity"],
                        0
                    ),

                "transaction_date":
                    row["transaction_date"]
                    or "",

                "transaction_time":
                    row["transaction_time"]
                    or "",

                "department_id":
                    row["department_id"],

                "printer_asset_id":
                    row["printer_asset_id"],

                "user_id":
                    row["user_id"],

                "location_id":
                    row["location_id"],

                "remarks":
                    row["remarks"]
                    or ""
            })

        return jsonify({

            "success":
                True,

            "period_type":
                period_type,

            "period_value":
                value,

            "total_transactions":
                len(transactions),

            "total_added":
                total_added,

            "total_issued":
                total_issued,

            "total_returned":
                total_returned,

            "total_refilled":
                total_refilled,

            "transactions":
                transactions
        })

    finally:

        conn.close()


# =========================================================
# SETTINGS API - GET
# =========================================================

@app.route(
    "/api/settings",
    methods=["GET"]
)
def get_settings():

    row = get_settings_row()

    if row is None:

        return jsonify({})

    return jsonify({

        "smtp_host":
            row["smtp_server"]
            or "",

        "smtp_server":
            row["smtp_server"]
            or "",

        "smtp_port":
            safe_int(
                row["smtp_port"],
                587
            ),

        "smtp_security":
            (
                clean_text(
                    row["smtp_security"]
                ).upper()
                if "smtp_security" in row.keys()
                and clean_text(
                    row["smtp_security"]
                ).upper() in ("NONE", "SSL", "TLS")
                else (
                    "SSL"
                    if safe_int(
                        row["smtp_port"],
                        587
                    ) == 465
                    else "TLS"
                )
            ),

        "smtp_username":
            row["smtp_username"]
            or "",

        "sender_email":
            row["sender_email"]
            or "",

        "alert_email":
            row["receiver_email"]
            or "",

        "alert_email_2":
            row["receiver_email_2"]
            or ""
            if "receiver_email_2" in row.keys()
            else "",

        "low_stock_alert":
            safe_int(
                row["low_stock_enabled"],
                1
            ),

        "danger_alert":
            safe_int(
                row["danger_stock_enabled"],
                1
            ),

        "dead_stock_alert":
            safe_int(
                row["dead_stock_enabled"],
                1
            ),

        "overstock_alert":
            safe_int(
                row["overstock_enabled"],
                1
            ),

        "default_minimum_stock":
            safe_int(
                row["default_minimum_stock"],
                5
            )
    })


# =========================================================
# SETTINGS API - SAVE
# =========================================================

@app.route(
    "/api/settings",
    methods=["POST"]
)
def save_settings():

    try:

        data = (
            request.get_json(
                silent=True
            )
            or {}
        )

        smtp_server = clean_text(
            data.get(
                "smtp_host",
                data.get(
                    "smtp_server",
                    ""
                )
            )
        )

        smtp_port = safe_int(
            data.get(
                "smtp_port",
                587
            ),
            587
        )

        smtp_security = clean_text(
            data.get(
                "smtp_security",
                ""
            )
        ).upper()

        if smtp_security not in (
            "NONE",
            "SSL",
            "TLS"
        ):
            smtp_security = (
                "SSL"
                if smtp_port == 465
                else "TLS"
            )

        smtp_username = clean_text(
            data.get(
                "smtp_username",
                ""
            )
        )

        smtp_password = str(
            data.get(
                "smtp_password",
                ""
            )
        )

        sender_email = clean_text(
            data.get(
                "sender_email",
                ""
            )
        )

        receiver_email = clean_text(
            data.get(
                "alert_email",
                data.get(
                    "receiver_email",
                    ""
                )
            )
        )

        has_receiver_email_2 = (
            "alert_email_2" in data
            or "receiver_email_2" in data
        )

        receiver_email_2 = clean_text(
            data.get(
                "alert_email_2",
                data.get(
                    "receiver_email_2",
                    ""
                )
            )
        ) if has_receiver_email_2 else None

        low_stock = data.get(
            "low_stock_alert",
            data.get(
                "low_stock_enabled",
                1
            )
        )

        danger_stock = data.get(
            "danger_alert",
            data.get(
                "danger_stock_enabled",
                1
            )
        )

        dead_stock = data.get(
            "dead_stock_alert",
            data.get(
                "dead_stock_enabled",
                1
            )
        )

        overstock = data.get(
            "overstock_alert",
            data.get(
                "overstock_enabled",
                1
            )
        )

        minimum_stock = safe_int(
            data.get(
                "default_minimum_stock",
                5
            ),
            5
        )

        if minimum_stock < 1:

            minimum_stock = 5

        def bool_value(value):

            return (
                1
                if str(value).lower()
                in (
                    "1",
                    "true",
                    "yes",
                    "on"
                )
                else 0
            )

        settings_data = {

            "smtp_server":
                smtp_server,

            "smtp_port":
                smtp_port,

            "smtp_security":
                smtp_security,

            "smtp_username":
                smtp_username,

            "smtp_password":
                smtp_password,

            "sender_email":
                sender_email,

            "receiver_email":
                receiver_email,

            "low_stock_enabled":
                bool_value(
                    low_stock
                ),

            "danger_stock_enabled":
                bool_value(
                    danger_stock
                ),

            "dead_stock_enabled":
                bool_value(
                    dead_stock
                ),

            "overstock_enabled":
                bool_value(
                    overstock
                ),

            "default_minimum_stock":
                minimum_stock
        }

        if has_receiver_email_2:
            settings_data["receiver_email_2"] = receiver_email_2

        save_settings_to_db(
            settings_data
        )

        return jsonify({

            "success":
                True,

            "message":
                "Settings saved successfully."
        })

    except Exception as error:

        print(
            "SAVE SETTINGS ERROR:",
            error
        )

        return jsonify({

            "success":
                False,

            "error":
                str(error)

        }), 500


# =========================================================
# TEST EMAIL
# =========================================================

@app.route(
    "/api/test-email",
    methods=["POST"]
)
def test_email():

    config = get_email_config()

    if config is None:

        return jsonify({
            "success": False,
            "error":
                "Email configuration not found."
        }), 400

    smtp_server = clean_text(
        config.get(
            "smtp_server",
            ""
        )
    )

    smtp_port = safe_int(
        config.get(
            "smtp_port",
            587
        ),
        587
    )

    smtp_username = clean_text(
        config.get(
            "smtp_username",
            ""
        )
    )

    smtp_password = str(
        config.get(
            "smtp_password",
            ""
        )
    )

    sender_email = clean_text(
        config.get(
            "sender_email",
            ""
        )
    )

    receiver_email = clean_text(
        config.get(
            "receiver_email",
            ""
        )
    )

    receiver_email_2 = clean_text(
        config.get(
            "receiver_email_2",
            ""
        )
    )

    recipients = [receiver_email]

    if (
        receiver_email_2
        and receiver_email_2.lower()
        != receiver_email.lower()
    ):
        recipients.append(receiver_email_2)

    smtp_security = clean_text(
        config.get(
            "smtp_security",
            ""
        )
    ).strip().upper()

    if smtp_security not in (
        "NONE",
        "TLS",
        "SSL"
    ):
        smtp_security = (
            "SSL"
            if smtp_port == 465
            else "TLS"
        )

    if not smtp_server:

        return jsonify({
            "success": False,
            "error":
                "SMTP server is not configured."
        }), 400

    # Username/password are required only for TLS/SSL.
    # For NONE, plain SMTP is allowed.
    if smtp_security in ("TLS", "SSL") and not smtp_username:

        return jsonify({
            "success": False,
            "error":
                "SMTP username is not configured."
        }), 400

    if smtp_security in ("TLS", "SSL") and not smtp_password:

        return jsonify({
            "success": False,
            "error":
                "SMTP password is not configured."
        }), 400

    if not receiver_email:

        return jsonify({
            "success": False,
            "error":
                "Receiver email is not configured."
        }), 400
    if not sender_email:

        sender_email = smtp_username

    email = EmailMessage()

    email["Subject"] = (
        "Smart Toner Management System - "
        "Test Email"
    )

    email["From"] = sender_email
    email["To"] = ", ".join(recipients)

    email.set_content(
        """
Smart Toner Management System

SMTP test email successful.

Your email settings are working correctly.
""".strip()
    )

    try:

        send_email_message(
            email,
            config
        )

        try:

            log_email_alert(
                None,
                "SYSTEM",
                "TEST",
                ", ".join(recipients),
                "SENT",
                "Test email sent successfully."
            )

        except Exception:

            pass

        return jsonify({

            "success":
                True,

            "message":
                "Test email sent successfully."
        })

    except Exception as error:

        error_message = str(
            error
        )

        try:

            log_email_alert(
                None,
                "SYSTEM",
                "TEST",
                ", ".join(recipients),
                "FAILED",
                error_message
            )

        except Exception:

            pass

        return jsonify({

            "success":
                False,

            "error":
                error_message

        }), 500


# =========================================================
# EMAIL HISTORY
# =========================================================

@app.route(
    "/api/email-alerts"
)
def email_alerts_api():

    conn = get_db_connection()

    try:

        rows = conn.execute(
            """
            SELECT
                id,
                toner_id,
                toner_model,
                alert_type,
                recipient,
                email_status,
                sent_date,
                message
            FROM email_alerts
            ORDER BY id DESC
            """
        ).fetchall()

        alerts = []

        for row in rows:

            alerts.append({

                "id":
                    row["id"],

                "toner_id":
                    row["toner_id"],

                "toner_model":
                    row["toner_model"]
                    or "",

                "alert_type":
                    row["alert_type"]
                    or "",

                "recipient":
                    row["recipient"]
                    or "",

                "email_status":
                    row["email_status"]
                    or "",

                "sent_date":
                    row["sent_date"]
                    or "",

                "message":
                    row["message"]
                    or ""
            })

        return jsonify({

            "success":
                True,

            "total_alerts":
                len(alerts),

            "alerts":
                alerts
        })

    finally:

        conn.close()


# =========================================================
# MASTER USERS
# =========================================================

@app.route(
    "/api/master/users"
)
def master_users_api():

    conn = get_db_connection()

    try:

        rows = conn.execute(
            """
            SELECT
                id,
                full_name,
                employee_code,
                email,
                department_id,
                location_id,
                active,
                created_at
            FROM users
            WHERE active = 1
            ORDER BY full_name
            """
        ).fetchall()

        users = []

        for row in rows:

            users.append({

                "id":
                    row["id"],

                "full_name":
                    row["full_name"]
                    or "",

                "name":
                    row["full_name"]
                    or "",

                "employee_code":
                    row["employee_code"]
                    or "",

                "email":
                    row["email"]
                    or "",

                "department_id":
                    row["department_id"],

                "location_id":
                    row["location_id"],

                "active":
                    row["active"]
            })

        return jsonify({

            "success":
                True,

            "users":
                users,

            "total_users":
                len(users)
        })

    finally:

        conn.close()


# =========================================================
# MASTER LOCATIONS
# =========================================================

@app.route(
    "/api/master/locations"
)
def master_locations_api():

    conn = get_db_connection()

    try:

        rows = conn.execute(
            """
            SELECT
                id,
                location_name,
                location_code,
                active,
                created_at
            FROM locations
            WHERE active = 1
            ORDER BY location_name
            """
        ).fetchall()

        locations = []

        for row in rows:

            locations.append({

                "id":
                    row["id"],

                "location_name":
                    row["location_name"]
                    or "",

                "name":
                    row["location_name"]
                    or "",

                "location_code":
                    row["location_code"]
                    or "",

                "active":
                    row["active"]
            })

        return jsonify({

            "success":
                True,

            "locations":
                locations,

            "total_locations":
                len(locations)
        })

    finally:

        conn.close()




# =========================================================
# CREATE MASTER USER / LOCATION RECORDS
# =========================================================

@app.route("/api/master/users/add", methods=["POST"])
def add_master_user_api():
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        full_name = clean_text(data.get("full_name"))
        employee_code = clean_text(data.get("employee_code"))
        email = clean_text(data.get("email"))
        location_id = safe_int(data.get("location_id"), 0) or None
        department_id = safe_int(data.get("department_id"), 0) or None

        if not full_name:
            return jsonify({"success": False, "error": "User name is required."}), 400

        conn = get_db_connection()
        existing = conn.execute(
            "SELECT id FROM users WHERE LOWER(TRIM(full_name)) = LOWER(TRIM(?)) AND active = 1 LIMIT 1",
            (full_name,)
        ).fetchone()
        if existing:
            return jsonify({"success": False, "error": "This active user already exists."}), 409

        if location_id is not None and conn.execute(
            "SELECT id FROM locations WHERE id = ? AND active = 1", (location_id,)
        ).fetchone() is None:
            return jsonify({"success": False, "error": "Selected location is not active."}), 400

        now = current_datetime()
        cursor = conn.execute(
            """INSERT INTO users
               (full_name, employee_code, email, department_id, location_id, active, created_at)
               VALUES (?, ?, ?, ?, ?, 1, ?)""",
            (full_name, employee_code, email, department_id, location_id, now)
        )
        conn.commit()
        return jsonify({
            "success": True,
            "message": "User added successfully.",
            "user": {
                "id": cursor.lastrowid,
                "full_name": full_name,
                "name": full_name,
                "employee_code": employee_code,
                "email": email,
                "location_id": location_id,
                "active": 1,
                "created_at": now
            }
        }), 201
    except Exception as error:
        if conn is not None:
            conn.rollback()
        app.logger.exception("ADD MASTER USER ERROR")
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/api/master/users/delete", methods=["POST"])
def delete_master_user_api():
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        user_id = safe_int(data.get("user_id"), 0)

        if user_id <= 0:
            return jsonify({"success": False, "error": "User ID is required."}), 400

        conn = get_db_connection()
        user = conn.execute(
            "SELECT id, full_name FROM users WHERE id = ? AND active = 1 LIMIT 1",
            (user_id,)
        ).fetchone()

        if user is None:
            return jsonify({"success": False, "error": "User not found."}), 404

        conn.execute(
            "UPDATE users SET active = 0 WHERE id = ?",
            (user_id,)
        )
        conn.commit()

        return jsonify({
            "success": True,
            "message": f"User '{user['full_name']}' was removed successfully."
        })
    except Exception as error:
        if conn is not None:
            conn.rollback()
        app.logger.exception("DELETE MASTER USER ERROR")
        return jsonify({"success": False, "error": str(error)}), 500
    finally:
        if conn is not None:
            conn.close()


@app.route("/api/master/locations/add", methods=["POST"])
def add_master_location_api():
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        location_name = clean_text(data.get("location_name"))
        location_code = clean_text(data.get("location_code"))
        if not location_name:
            return jsonify({"success": False, "error": "Location name is required."}), 400

        conn = get_db_connection()
        existing = conn.execute(
            "SELECT id FROM locations WHERE LOWER(TRIM(location_name)) = LOWER(TRIM(?)) LIMIT 1",
            (location_name,)
        ).fetchone()
        if existing:
            return jsonify({"success": False, "error": "This location already exists."}), 409

        now = current_datetime()
        cursor = conn.execute(
            "INSERT INTO locations (location_name, location_code, active, created_at) VALUES (?, ?, 1, ?)",
            (location_name, location_code, now)
        )
        conn.commit()
        return jsonify({
            "success": True,
            "message": "Location added successfully.",
            "location": {
                "id": cursor.lastrowid,
                "location_name": location_name,
                "name": location_name,
                "location_code": location_code,
                "active": 1,
                "created_at": now
            }
        }), 201
    except sqlite3.IntegrityError:
        if conn is not None:
            conn.rollback()
        return jsonify({"success": False, "error": "This location already exists."}), 409
    except Exception as error:
        if conn is not None:
            conn.rollback()
        app.logger.exception("ADD MASTER LOCATION ERROR")
        return jsonify({"success": False, "error": str(error)}), 500
    finally:
        if conn is not None:
            conn.close()


# =========================================================
# SIMPLE COMPATIBILITY ROUTES
# =========================================================

@app.route(
    "/api/users"
)
def users_api():

    return master_users_api()


@app.route(
    "/api/locations"
)
def locations_api():

    return master_locations_api()


# =========================================================
# FORGOT PASSWORD
# =========================================================

@app.route(
    "/forgot-password",
    methods=["GET", "POST"]
)
def forgot_password():

    if request.method == "POST":

        username = clean_text(
            request.form.get(
                "username"
            )
        )

        if not username:

            return render_template(
                "forgot_password.html",
                error=
                    "Username is required."
            )

        conn = get_db_connection()

        try:

            user = conn.execute(
                """
                SELECT *
                FROM login_users
                WHERE username = ?
                AND active = 1
                """,
                (username,)
            ).fetchone()

        finally:

            conn.close()

        if user is None:

            return render_template(
                "forgot_password.html",
                error=
                    "User account not found."
            )

        config = get_email_config()

        if (
            config is None
            or not config["smtp_server"]
            or not config["smtp_username"]
            or not config["smtp_password"]
            or not config["receiver_email"]
        ):

            return render_template(
                "forgot_password.html",
                error=
                    "Email configuration is incomplete."
            )

        otp = str(
            secrets.randbelow(
                900000
            ) + 100000
        )

        session["reset_user_id"] = (
            user["id"]
        )

        session["reset_username"] = (
            username
        )

        session["reset_otp"] = otp

        session["reset_otp_created"] = (
            datetime.now().timestamp()
        )

        session["reset_otp_attempts"] = 0

        message = EmailMessage()

        message["Subject"] = (
            "Smart Toner Management System - "
            "Password Reset OTP"
        )

        message["From"] = (
            config["sender_email"]
        )

        message["To"] = (
            config["receiver_email"]
        )

        message.set_content(
            f"""
Smart Toner Management System

Password Reset OTP
==================

Username : {username}

OTP : {otp}

OTP validity: 10 minutes.

Do not share this OTP with anyone.
""".strip()
        )

        try:

            send_email_message(
                message,
                config
            )

            return redirect(
                url_for(
                    "verify_otp"
                )
            )

        except Exception as error:

            print(
                "FORGOT PASSWORD ERROR:",
                error
            )

            return render_template(
                "forgot_password.html",
                error=
                    "Unable to send OTP email."
            )

    return render_template(
        "forgot_password.html"
    )


# =========================================================
# VERIFY OTP
# =========================================================

@app.route(
    "/verify-otp",
    methods=["GET", "POST"]
)
def verify_otp():

    if request.method == "POST":

        entered_otp = clean_text(
            request.form.get(
                "otp"
            )
        )

        created = session.get(
            "reset_otp_created"
        )

        if not created:

            return render_template(
                "verify_otp.html",
                error=
                    "OTP expired."
            )

        if (
            datetime.now().timestamp()
            - created
        ) > 600:

            return render_template(
                "verify_otp.html",
                error=
                    "OTP expired."
            )

        attempts = session.get(
            "reset_otp_attempts",
            0
        )

        if attempts >= 5:

            return render_template(
                "verify_otp.html",
                error=
                    "Too many incorrect attempts."
            )

        stored_otp = str(
            session.get(
                "reset_otp",
                ""
            )
        )

        if not secrets.compare_digest(
            entered_otp,
            stored_otp
        ):

            session[
                "reset_otp_attempts"
            ] = attempts + 1

            return render_template(
                "verify_otp.html",
                error=
                    "Invalid OTP."
            )

        session[
            "reset_verified"
        ] = True

        return redirect(
            url_for(
                "reset_password"
            )
        )

    return render_template(
        "verify_otp.html"
    )


# =========================================================
# RESET PASSWORD
# =========================================================

@app.route(
    "/reset-password",
    methods=["GET", "POST"]
)
def reset_password():

    if not session.get(
        "reset_verified"
    ):

        return redirect(
            url_for(
                "forgot_password"
            )
        )

    user_id = session.get(
        "reset_user_id"
    )

    if not user_id:

        session.clear()

        return redirect(
            url_for(
                "login"
            )
        )

    if request.method == "POST":

        new_password = request.form.get(
            "new_password",
            ""
        )

        confirm_password = request.form.get(
            "confirm_password",
            ""
        )

        if len(
            new_password
        ) < 8:

            return render_template(
                "reset_password.html",
                error=
                    "Password must be at least 8 characters."
            )

        if (
            new_password
            != confirm_password
        ):

            return render_template(
                "reset_password.html",
                error=
                    "Passwords do not match."
            )

        conn = get_db_connection()

        try:

            conn.execute(
                """
                UPDATE login_users
                SET password_hash = ?
                WHERE id = ?
                """,
                (
                    generate_password_hash(
                        new_password
                    ),
                    user_id
                )
            )

            conn.commit()

        except Exception:

            conn.rollback()

            raise

        finally:

            conn.close()

        session.clear()

        return render_template(
            "reset_password.html",
            success=
                "Password reset successfully."
        )

    return render_template(
        "reset_password.html"
    )


# =========================================================
# CHANGE PASSWORD
# =========================================================

@app.route(
    "/change-password",
    methods=["GET", "POST"]
)
def change_password():
    """Allow only the currently signed-in user to change their password."""
    if not session.get("logged_in") or not session.get("login_user_id"):
        return redirect(url_for("login"))

    error = None
    success = None

    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not current_password:
            error = "Current password is required."
        elif len(new_password) < 8:
            error = "New password must be at least 8 characters."
        elif new_password != confirm_password:
            error = "New passwords do not match."
        else:
            conn = get_db_connection()
            try:
                user = conn.execute(
                    "SELECT id, password_hash FROM login_users WHERE id = ? AND active = 1",
                    (session["login_user_id"],)
                ).fetchone()

                if user is None:
                    session.clear()
                    return redirect(url_for("login"))
                if not check_password_hash(user["password_hash"], current_password):
                    error = "Current password is incorrect."
                else:
                    conn.execute(
                        "UPDATE login_users SET password_hash = ? WHERE id = ?",
                        (generate_password_hash(new_password), user["id"])
                    )
                    conn.commit()
                    success = "Password changed successfully."
            except Exception:
                conn.rollback()
                app.logger.exception("CHANGE PASSWORD ERROR")
                error = "Unable to change the password right now."
            finally:
                conn.close()

    return render_template(
        "change_password.html",
        error=error,
        success=success
    )


# =========================================================
# APPLICATION START
# =========================================================

initialize_database()


if __name__ == "__main__":

    print()

    print("=" * 65)

    print(
        "        SMART TONER MANAGEMENT SYSTEM"
    )

    print("=" * 65)

    print()

    print(
        " Login     : "
        "http://127.0.0.1:5000/login"
    )

    print(
        " Dashboard : "
        "http://127.0.0.1:5000/"
    )

    print(
        " Issue / Return / Refill : "
        "http://127.0.0.1:5000/issue-return-refill"
    )

    print(
        " Add Toner : "
        "http://127.0.0.1:5000/add-toner"
    )

    print(
        " Toners    : "
        "http://127.0.0.1:5000/toners"
    )

    print(
        " Settings  : "
        "http://127.0.0.1:5000/settings"
    )

    print()

    print("=" * 65)

    print()
    print("Running app     :", os.path.abspath(__file__))
    print("Running database:", os.path.abspath(DATABASE))
    print("Refill delete   : POST /api/refill-record/delete")
    print()

    # Reloader disabled to reduce SQLite lock conflicts.
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True,
        use_reloader=False
    )
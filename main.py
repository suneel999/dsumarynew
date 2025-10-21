# imports
from flask import Flask, request, render_template, send_file, flash, redirect, url_for, session
from docxtpl import DocxTemplate
from dotenv import load_dotenv
from io import BytesIO
import os, tempfile, json, requests, re, time
from datetime import datetime
import pdfplumber
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = "supersecretkey"
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload size

# load environment variables from .env
load_dotenv()

# Configuration
# Removed disk-based saving directory
ALLOWED_EXTENSIONS = {'pdf'}
# os.makedirs(SAVED_DOCUMENTS_DIR, exist_ok=True)  # no longer needed

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_text_from_pdf(file_path):
    with pdfplumber.open(file_path) as pdf:
        return "\n".join([page.extract_text() for page in pdf.pages if page.extract_text()])


def get_json_from_pdf_via_gemini(pdf_path, max_retries=3, retry_delay=2):
    pdf_text = extract_text_from_pdf(pdf_path)

    prompt = f"""
You are a strict JSON generator for medical discharge summaries. ONLY respond with raw JSON.

Convert this discharge summary into JSON with the format:
{{
  "name": "Patient's full name",
  "age/gender": "Age and gender",
  "ad1": "Address line 1",
  "ad2": "Address line 2",
  "mob": "Mobile number",
  "admision_number": "Admission number",
  "umr": "Unique Medical Record number",
  "ward": "Ward name/number",
  "admission_date": "YYYY-MM-DD",
  "discharge_date": "YYYY-MM-DD",
  "Diagnosis": ["Primary diagnosis", "Secondary diagnosis", "ADVICE: MEDICAL MANAGEMENT"],  
  "Riskfactors": ["Hypertension", "Hypothyroidism"],  
  "PastHistory": ["Past history 1", "Past history 2"],  
  "ChiefComplaints": "Chief complaints text",
  "Course": ["Hospital course point 1", "Point 2"],
  "Vitals": {{
    "TEMP": "Temperature",
    "PR": "Pulse rate",
    "BP": "Blood pressure",
    "SPo2": "Oxygen saturation",
    "RR": "Respiratory rate"
  }},
  "Examination": {{
    "CVS": "CVS findings",
    "RS": "RS findings",
    "CNS": "CNS findings",
    "PA": "PA findings"
  }},
  "Medications": [
    {{
      "form": "Tab/Cap/Inj",
      "name": "Medicine name",
      "dosage": "10MG",
      "freq": "ONCE DAILY",
      "time": "8PM AFTER FOOD"
    }}
  ]
}}

RULES:
1. If PDF shows "Risk Factors / Past History" combined → split into Riskfactors and PastHistory arrays.
2. Must include "ADVICE: MEDICAL MANAGEMENT" in Diagnosis.
3. If any field is missing → return "N/A" (not None).
4. Medications must include form (Cap/Tab/Inj) with name.
5. Output only raw JSON.
PDF text:
\"\"\"
{pdf_text}
\"\"\"
"""

    if not GEMINI_API_KEY:
        raise RuntimeError("Missing GEMINI_API_KEY environment variable")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    body = {"contents": [{"parts": [{"text": prompt}]}]}

    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=body, timeout=20)
            response.raise_for_status()
            content = response.json()

            candidate = content["candidates"][0]
            raw_text = candidate["content"]["parts"][0]["text"]

            match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            clean_json = match.group(0)
            return json.loads(clean_json)

        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
            raise


def format_multiline_field(content):
    if not content:
        return ""
    return "\n".join(content) if isinstance(content, list) else str(content)


def parse_multiline(text):
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def validate_json_data(data):
    required_fields = ['name', 'age/gender', 'admission_date', 'discharge_date']
    for field in required_fields:
        if not data.get(field):
            raise ValueError(f"Missing required field: {field}")
    return True


@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        if 'pdf' not in request.files:
            flash("No file part", "danger")
            return redirect(request.url)

        file = request.files['pdf']
        if file.filename == '' or not allowed_file(file.filename):
            flash("Invalid file", "danger")
            return redirect(request.url)

        try:
            filename = secure_filename(file.filename)
            temp_path = os.path.join(tempfile.gettempdir(), filename)
            file.save(temp_path)

            # Extract JSON and go to review step instead of immediate generation
            json_data = get_json_from_pdf_via_gemini(temp_path)
            session['json_data'] = json_data
            flash("PDF processed. Please review and edit before generating.", "info")
            return redirect(url_for("review"))

        except Exception as e:
            flash(f"❌ Error: {str(e)}", "danger")
            return redirect(request.url)

    return render_template("index.html")


@app.route("/review", methods=["GET", "POST"])
def review():
    # GET stays the same: loads session data and renders edit_form.html
    # POST: only medical content is edited; personal info stays from session
    if request.method == "GET":
        data = session.get('json_data')
        if not data:
            flash("No data to review. Please upload a PDF first.", "warning")
            return redirect(url_for("home"))
        return render_template("edit_form.html", data=data)

    if request.method == "POST":
        data = session.get('json_data') or {}
        edited = {
            # Personal info stays from extracted data; review form only edits medical sections
            "umr": data.get("umr", "N/A"),
            "name": data.get("name", "N/A"),
            "age/gender": data.get("age/gender", "N/A"),
            "ad1": data.get("ad1", "N/A"),
            "ad2": data.get("ad2", "N/A"),
            "mob": data.get("mob", "N/A"),
            "admision_number": data.get("admision_number", "N/A"),
            "ward": data.get("ward", "N/A"),
            "admission_date": data.get("admission_date", "N/A"),
            "discharge_date": data.get("discharge_date", "N/A"),
            "Diagnosis": parse_multiline(request.form.get("Diagnosis", format_multiline_field(data.get("Diagnosis", "")))),
            "Riskfactors": parse_multiline(request.form.get("Riskfactors", format_multiline_field(data.get("Riskfactors", "")))),
            "PastHistory": parse_multiline(request.form.get("PastHistory", format_multiline_field(data.get("PastHistory", "")))),
            "ChiefComplaints": request.form.get("ChiefComplaints", data.get("ChiefComplaints", "N/A")),
            "Course": parse_multiline(request.form.get("Course", format_multiline_field(data.get("Course", "")))),
            "Vitals": {
                "TEMP": request.form.get("TEMP", data.get("Vitals", {}).get("TEMP", "N/A")),
                "PR": request.form.get("PR", data.get("Vitals", {}).get("PR", "N/A")),
                "BP": request.form.get("BP", data.get("Vitals", {}).get("BP", "N/A")),
                "SPo2": request.form.get("SPo2", data.get("Vitals", {}).get("SPo2", "N/A")),
                "RR": request.form.get("RR", data.get("Vitals", {}).get("RR", "N/A")),
            },
            "Examination": {
                "CVS": request.form.get("CVS", data.get("Examination", {}).get("CVS", "N/A")),
                "RS": request.form.get("RS", data.get("Examination", {}).get("RS", "N/A")),
                "CNS": request.form.get("CNS", data.get("Examination", {}).get("CNS", "N/A")),
                "PA": request.form.get("PA", data.get("Examination", {}).get("PA", "N/A")),
            },
            "Medications": data.get("Medications", [])
        }

        # Robust init: Medications must be a list (handle missing/None/"N/A"/wrong type)
        meds_src = data.get("Medications")
        edited["Medications"] = meds_src if isinstance(meds_src, list) else []

        # Ensure diagnosis includes "ADVICE: MEDICAL MANAGEMENT"
        diagnosis = edited.get("Diagnosis", [])
        if isinstance(diagnosis, str):
            diagnosis = [diagnosis]
        diagnosis = list(dict.fromkeys(diagnosis))
        if not any("ADVICE: MEDICAL MANAGEMENT" in d.upper() for d in diagnosis):
            diagnosis.append("ADVICE: MEDICAL MANAGEMENT")
        edited["Diagnosis"] = diagnosis

        # Safety net: make sure it's still a list before we append
        if not isinstance(edited.get("Medications"), list):
            edited["Medications"] = []

        # Collect medications (up to 10; form forced uppercase)
        for i in range(1, 11):
            form_val = (request.form.get(f"TAB{i}_form", "") or "").strip().upper()
            name_val = request.form.get(f"TAB{i}_name", "").strip()
            dosage = request.form.get(f"DOSAGE{i}", "").strip()
            freq = request.form.get(f"FREQ{i}", "").strip()
            time_val = request.form.get(f"TOM{i}", "").strip()
            if any([form_val, name_val, dosage, freq, time_val]):
                edited["Medications"].append({
                    "form": form_val or "",
                    "name": name_val or "",
                    "dosage": dosage or "N/A",
                    "freq": freq or "N/A",
                    "time": time_val or "N/A",
                })

        try:
            # Validate required fields
            validate_json_data(edited)

            # Dates formatting
            admission_date = edited.get("admission_date", "")
            discharge_date = edited.get("discharge_date", "")
            try:
                if admission_date:
                    admission_date = datetime.strptime(admission_date, "%Y-%m-%d").strftime("%d-%b-%Y")
                if discharge_date:
                    discharge_date = datetime.strptime(discharge_date, "%Y-%m-%d").strftime("%d-%b-%Y")
            except ValueError:
                pass

            # Riskfactors + PastHistory
            risk_factors = edited.get("Riskfactors", [])
            if isinstance(risk_factors, str):
                risk_factors = [risk_factors]
            past_history = edited.get("PastHistory", [])
            if isinstance(past_history, str):
                past_history = [past_history]
            combined_history = list(dict.fromkeys(risk_factors + past_history))

            # Build context
            doc = DocxTemplate("template.docx")
            context = {
                "umr": edited.get("umr", "N/A"),
                "name": edited.get("name", "N/A").title(),
                "age": edited.get("age/gender", "N/A"),
                "ad1": edited.get("ad1", "N/A").title(),
                "ad2": edited.get("ad2", "N/A").title(),
                "mob": edited.get("mob", "N/A"),
                "admision": edited.get("admision_number", "N/A"),
                "ward": edited.get("ward", "N/A").upper(),
                "admit": admission_date,
                "discharge": discharge_date,
                "Diagnosis": format_multiline_field(edited.get("Diagnosis", [])),
                "ChiefComplaints": format_multiline_field(edited.get("ChiefComplaints", "N/A")).upper(),
                "Riskfactors": format_multiline_field(combined_history),
                "Course": format_multiline_field(edited.get("Course", "N/A")),
                "TEMP": edited.get("Vitals", {}).get("TEMP", "N/A"),
                "BP": edited.get("Vitals", {}).get("BP", "N/A"),
                "PR": edited.get("Vitals", {}).get("PR", "N/A"),
                "SPo2": edited.get("Vitals", {}).get("SPo2", "N/A"),
                "RR": edited.get("Vitals", {}).get("RR", "N/A"),
                "CVS": edited.get("Examination", {}).get("CVS", "N/A"),
                "RS": edited.get("Examination", {}).get("RS", "N/A"),
                "CNS": edited.get("Examination", {}).get("CNS", "N/A"),
                "PA": edited.get("Examination", {}).get("PA", "N/A"),
                "current_date": datetime.now().strftime("%d-%b-%Y"),
                "current_time": datetime.now().strftime("%I:%M %p")
            }

            # Add common aliases to improve template compatibility
            context.update({
                # Uppercase/basic aliases
                "UMR": context["umr"],
                "NAME": context["name"],
                "AGE_GENDER": edited.get("age/gender", "N/A"),
                "AD1": context["ad1"],
                "AD2": context["ad2"],
                "MOB": context["mob"],
                "WARD_NAME": context["ward"],

                # Admission/discharge naming variants
                "ADMISSION": context["admit"],
                "DISCHARGE": context["discharge"],
                "ADMISSION_DATE": context["admit"],
                "DISCHARGE_DATE": context["discharge"],

                # Number variants (note: original JSON uses 'admision_number')
                "ADMISSION_NUMBER": context["admision"],
                "ADMISSION_NO": context["admision"],

                # Vitals variants
                "SPO2": context["SPo2"],  # some templates use 'SPO2'
            })

            # Helpful debug: see available keys in logs
            app.logger.info(f"Docx context keys: {sorted(list(context.keys()))}")

            # Medications placeholders
            meds = edited.get("Medications") or []
            for i in range(10):
                med = meds[i] if i < len(meds) else {}
                form_val = med.get("form", "").strip()
                name_val = med.get("name", "").strip()
                full_name = f"{form_val} {name_val}".strip() if (form_val or name_val) else ""
                context[f"TAB{i + 1}"] = full_name
                context[f"DOSAGE{i + 1}"] = med.get("dosage", "N/A") if med.get("dosage") else "N/A"
                context[f"FREQ{i + 1}"] = med.get("freq", "N/A")
                context[f"TOM{i + 1}"] = med.get("time", "N/A")

            # Render and stream doc (no disk write)
            doc.render(context)
            output_filename = f"Discharge_{context['name'].replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
            buffer = BytesIO()
            docx_obj = doc.get_docx()
            docx_obj.save(buffer)
            buffer.seek(0)

            flash("✅ Document generated successfully!", "success")
            return send_file(
                buffer,
                as_attachment=True,
                download_name=output_filename,
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )

        except Exception as e:
            flash(f"❌ Error during generation: {str(e)}", "danger")
            return redirect(url_for("review"))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", "8000")), debug=False)

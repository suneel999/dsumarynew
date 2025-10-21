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
    # GET request renders the form with data from the session
    if request.method == "GET":
        data = session.get('json_data')
        if not data:
            flash("No data to review. Please upload a PDF first.", "warning")
            return redirect(url_for("home"))
        return render_template("edit_form.html", data=data)

    # POST request uses the original, reliable logic to generate the document
    if request.method == "POST":
        try:
            # 1. Combine original data from session with edits from the form
            # This mirrors the structure of the original `json_data` object
            session_data = session.get('json_data') or {}
            edited_data = {
                # Keep personal info from the original upload
                "umr": session_data.get("umr", "N/A"),
                "name": session_data.get("name", "N/A"),
                "age/gender": session_data.get("age/gender", "N/A"),
                "ad1": session_data.get("ad1", "N/A"),
                "ad2": session_data.get("ad2", "N/A"),
                "mob": session_data.get("mob", "N/A"),
                "admision_number": session_data.get("admision_number", "N/A"),
                "ward": session_data.get("ward", "N/A"),
                "admission_date": session_data.get("admission_date", "N/A"),
                "discharge_date": session_data.get("discharge_date", "N/A"),

                # Get edited medical content from the form
                "Diagnosis": parse_multiline(request.form.get("Diagnosis")),
                "Riskfactors": parse_multiline(request.form.get("Riskfactors")),
                "PastHistory": parse_multiline(request.form.get("PastHistory")),
                "ChiefComplaints": request.form.get("ChiefComplaints"),
                "Course": parse_multiline(request.form.get("Course")),
                "Vitals": {
                    "TEMP": request.form.get("TEMP"), "PR": request.form.get("PR"),
                    "BP": request.form.get("BP"), "SPo2": request.form.get("SPo2"),
                    "RR": request.form.get("RR"),
                },
                "Examination": {
                    "CVS": request.form.get("CVS"), "RS": request.form.get("RS"),
                    "CNS": request.form.get("CNS"), "PA": request.form.get("PA"),
                },
                "Medications": [] # This will be filled next
            }

            # Collect medications from the form
            for i in range(1, 11):
                form_val = (request.form.get(f"TAB{i}_form", "") or "").strip().upper()
                name_val = request.form.get(f"TAB{i}_name", "").strip()
                if name_val: # Only add if a medication name is present
                    edited_data["Medications"].append({
                        "form": form_val, "name": name_val,
                        "dosage": request.form.get(f"DOSAGE{i}", "N/A"),
                        "freq": request.form.get(f"FREQ{i}", "N/A"),
                        "time": request.form.get(f"TOM{i}", "N/A"),
                    })

            # 2. Validate the combined data
            validate_json_data(edited_data)
            doc = DocxTemplate("template.docx")

            # 3. Prepare dates, diagnosis, and history just like the original code
            admission_date = edited_data.get("admission_date", "")
            discharge_date = edited_data.get("discharge_date", "")
            try:
                if admission_date:
                    admission_date = datetime.strptime(admission_date, "%Y-%m-%d").strftime("%d-%b-%Y")
                if discharge_date:
                    discharge_date = datetime.strptime(discharge_date, "%Y-%m-%d").strftime("%d-%b-%Y")
            except ValueError:
                pass

            diagnosis = list(dict.fromkeys(edited_data.get("Diagnosis", [])))
            if not any("ADVICE: MEDICAL MANAGEMENT" in d.upper() for d in diagnosis):
                diagnosis.append("ADVICE: MEDICAL MANAGEMENT")
            
            combined_history = list(dict.fromkeys(
                edited_data.get("Riskfactors", []) + edited_data.get("PastHistory", [])
            ))

            # 4. Build the context dictionary using the original structure
            context = {
                "umr": edited_data.get("umr", "N/A"),
                "name": edited_data.get("name", "N/A").title(),
                "age": edited_data.get("age/gender", "N/A"),
                "ad1": edited_data.get("ad1", "N/A").title(),
                "ad2": edited_data.get("ad2", "N/A").title(),
                "mob": edited_data.get("mob", "N/A"),
                "admision": edited_data.get("admision_number", "N/A"),
                "ward": edited_data.get("ward", "N/A").upper(),
                "admit": admission_date,
                "discharge": discharge_date,
                "Diagnosis": format_multiline_field(diagnosis),
                "ChiefComplaints": format_multiline_field(edited_data.get("ChiefComplaints", "N/A")).upper(),
                "Riskfactors": format_multiline_field(combined_history),
                "Course": format_multiline_field(edited_data.get("Course", "N/A")),
                "TEMP": edited_data.get("Vitals", {}).get("TEMP", "N/A"),
                "BP": edited_data.get("Vitals", {}).get("BP", "N/A"),
                "PR": edited_data.get("Vitals", {}).get("PR", "N/A"),
                "SPo2": edited_data.get("Vitals", {}).get("SPo2", "N/A"),
                "RR": edited_data.get("Vitals", {}).get("RR", "N/A"),
                "CVS": edited_data.get("Examination", {}).get("CVS", "N/A"),
                "RS": edited_data.get("Examination", {}).get("RS", "N/A"),
                "CNS": edited_data.get("Examination", {}).get("CNS", "N/A"),
                "PA": edited_data.get("Examination", {}).get("PA", "N/A"),
                "current_date": datetime.now().strftime("%d-%b-%Y"),
                "current_time": datetime.now().strftime("%I:%M %p")
            }

            meds = edited_data.get("Medications") or []
            for i in range(10):
                med = meds[i] if i < len(meds) else {}
                context[f"TAB{i + 1}"] = f"{med.get('form', '')} {med.get('name', '')}".strip()
                context[f"DOSAGE{i + 1}"] = med.get("dosage", "N/A")
                context[f"FREQ{i + 1}"] = med.get("freq", "N/A")
                context[f"TOM{i + 1}"] = med.get("time", "N/A")

            # 5. Render the document and stream it to the user
            # Build context
            doc = DocxTemplate("template.docx")

            # DEBUG: which variables does the template expose?
            try:
                missing = doc.get_undeclared_template_variables({
                    # provide a minimal context so it can compare
                    "umr": "", "name": "", "age": "", "ad1": "", "ad2": "", "mob": "",
                    "admision": "", "ward": "", "admit": "", "discharge": "",
                    "Diagnosis": "", "ChiefComplaints": "", "Riskfactors": "", "Course": "",
                    "TEMP": "", "BP": "", "PR": "", "SPo2": "", "RR": "",
                    "CVS": "", "RS": "", "CNS": "", "PA": "",
                    **{f"TAB{i}": "" for i in range(1, 11)},
                    **{f"DOSAGE{i}": "" for i in range(1, 11)},
                    **{f"FREQ{i}": "" for i in range(1, 11)},
                    **{f"TOM{i}": "" for i in range(1, 11)},
                })
                app.logger.info(f"Template variables DocxTemplate can see (missing subset): {sorted(list(missing))}")
            except Exception as e:
                app.logger.warning(f"Template variable scan failed: {e}")

            context = {
                "umr": edited_data.get("umr", "N/A"),
                "name": edited_data.get("name", "N/A").title(),
                "age": edited_data.get("age/gender", "N/A"),
                "ad1": edited_data.get("ad1", "N/A").title(),
                "ad2": edited_data.get("ad2", "N/A").title(),
                "mob": edited_data.get("mob", "N/A"),
                "admision": edited_data.get("admision_number", "N/A"),
                "ward": edited_data.get("ward", "N/A").upper(),
                "admit": admission_date,
                "discharge": discharge_date,
                "Diagnosis": format_multiline_field(diagnosis),
                "ChiefComplaints": format_multiline_field(edited_data.get("ChiefComplaints", "N/A")).upper(),
                "Riskfactors": format_multiline_field(combined_history),
                "Course": format_multiline_field(edited_data.get("Course", "N/A")),
                "TEMP": edited_data.get("Vitals", {}).get("TEMP", "N/A"),
                "BP": edited_data.get("Vitals", {}).get("BP", "N/A"),
                "PR": edited_data.get("Vitals", {}).get("PR", "N/A"),
                "SPo2": edited_data.get("Vitals", {}).get("SPo2", "N/A"),
                "RR": edited_data.get("Vitals", {}).get("RR", "N/A"),
                "CVS": edited_data.get("Examination", {}).get("CVS", "N/A"),
                "RS": edited_data.get("Examination", {}).get("RS", "N/A"),
                "CNS": edited_data.get("Examination", {}).get("CNS", "N/A"),
                "PA": edited_data.get("Examination", {}).get("PA", "N/A"),
                "current_date": datetime.now().strftime("%d-%b-%Y"),
                "current_time": datetime.now().strftime("%I:%M %p")
            }

            meds = edited_data.get("Medications") or []
            for i in range(10):
                med = meds[i] if i < len(meds) else {}
                context[f"TAB{i + 1}"] = f"{med.get('form', '')} {med.get('name', '')}".strip()
                context[f"DOSAGE{i + 1}"] = med.get("dosage", "N/A")
                context[f"FREQ{i + 1}"] = med.get("freq", "N/A")
                context[f"TOM{i + 1}"] = med.get("time", "N/A")

            # 5. Render the document and stream it to the user
            doc.render(context)
            output_filename = f"Discharge_{context['name'].replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
            buffer = BytesIO()
            doc.save(buffer)
            buffer.seek(0)
            
            flash("✅ Document generated successfully!", "success")
            return send_file(
                buffer,
                as_attachment=True,
                download_name=output_filename,
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )

        except Exception as e:
            app.logger.error(f"Error in /review [POST]: {e}", exc_info=True)
            flash(f"❌ Error during generation: {str(e)}", "danger")
            return redirect(url_for("review"))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", "8000")), debug=False)

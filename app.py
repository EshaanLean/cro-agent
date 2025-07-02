import os
import json
import csv
import genai  # Google Gemini API client
from flask import Flask, request, render_template_string, send_file

app = Flask(__name__)

UPLOAD_FOLDER = "manual_screenshots"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
CSV_FILE = "competitive_analysis_results.csv"

def flushprint(*args, **kwargs):
    print(*args, **kwargs, flush=True)

def url_to_key(url):
    return url.replace("https://", "www_").replace("http://", "www_").replace(".", "_").replace("/", "_").replace("?", "_").replace("=", "_")

default_prompt = """
As a digital marketing and CRO (Conversion Rate Optimization) expert, analyze the provided landing page screenshot and text content for the company '{provider_name}'.
Your goal is to populate a structured JSON object based on the visual and textual evidence.

**Webpage Information:**
- **Provider:** {provider_name}
- **URL:** {url}
{prompt_text_section}

**Instructions:**
Carefully examine the **screenshot** for visual layout, design elements, and "above the fold" content.
If text content is provided, use it to extract specific details and copy. If not, rely only on the screenshot.
Fill out the following JSON object.

If you cannot determine a value, use "Not Found" or "N/A".

**JSON Structure to Populate:**
{{
  "Platform": "{provider_name}",
  "LP Link": "{url}",
  "Main Offer": "Describe the main value proposition or product offering.",
  "Purchase or Lead Gen Form": "Classify the primary conversion goal. If the main button leads directly to a payment form, classify as 'Direct Purchase'. If it leads to a free sign-up, a free trial, or a form to request information/a demo, classify as 'Lead Generation'. If it is a simple sign-up to start a free course, classify as 'Low-friction sign-up'.",
  "Primary CTA": "Identify the most prominent, visually emphasized call-to-action button above the fold. This is usually the largest button with the brightest color. Provide its exact text.",
  "Above the Fold - Headline": "The main headline text visible at the top of the page.",
  "Above the Fold - Trust Elements": "List any trust signals visible without scrolling (e.g., logos of partners, ratings, student testimonials, 'Trusted by X users').",
  "Above the Fold - Other Elements": "List other key elements visible (e.g., sub-headlines, short descriptions, benefits).",
  "Above the Fold - Creative (Yes/No)": "Is there a prominent hero image, video, or illustration? (Yes/No)",
  "Above the Fold - Creative Type": "If yes, describe the creative (e.g., 'Hero image with testimonial', 'Course preview video', 'Illustration of data concepts').",
  "Above the Fold - Creative Position": "Where is the creative located? (e.g., 'Right side of the hero section', 'Background video').",
  "Above the Fold - # of CTAs": "Count all distinct call-to-action buttons AND text links (e.g., 'Enroll Now', 'Request Info', 'Financial aid available') visible above the fold.",
  "Above the Fold - CTA / Form Position": "Describe the position of the primary CTA or lead form.",
  "Primary CTA Just for Free Trial": "Does the primary CTA explicitly mention a free trial or is it for direct enrollment/purchase? (e.g., 'Start Free Trial', 'Enroll Now').",
  "Secondary CTA": "Identify the second-most prominent call-to-action. This could be a button with a less vibrant color, an outlined button, or a prominent text link like 'Book a Call' or 'Explore Syllabus'. Provide its exact text.",
  "Clickable Logo": "Is the main logo in the navigation bar clickable? (Assume Yes if it's standard practice).",
  "Navigation Bar": "Are there navigation links at the top of the page? (Yes/No)"
}}

Return ONLY the valid JSON object, with no other text, comments, or markdown formatting.
"""

strategy_prompt_template = """
You are a CRO and digital strategy agent. Your goal is to analyze {client_name}'s landing page for its {theme} landing page and identify strategic opportunities to improve conversion performance.

📌 **Client Landing Page:** {client_lp}

**Compare this page against these competitor landing pages:**
{competitor_urls}

You also have access to a detailed comparison table of all pages (in CSV format below) showing:
- Above-the-fold breakdowns (headline, CTA placement, navigation, creative)
- Below-the-fold content sections (curriculum, testimonials, pricing, etc.)
- I have also provided full-page screenshots of all pages during a previous step, which you should use for visual reference.

**Detailed Comparison Data (CSV):**
```csv
{data_string}

🎯 Your task:

Write a strategic summary for {client_name} — identifying how it can improve conversions through CRO and personalization, without changing its core offer (real-world projects, mentorship, career prep).

Create an Opportunity Table with these columns:

Opportunity

Why It Matters

Tactical Ideas

Rationale / Inspiration (note if it’s competitor-based or CRO best practice)

Summarize the strategic advantage these changes would unlock.

Present your response in clean, readable way.
"""


---

## 3. HTML Template

```python
HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>CRO Landing Page Analyzer</title>
    <style>
        body { font-family: Arial, sans-serif; margin:40px;}
        input[type="text"] { font-size: 14px; }
        textarea { width: 98%; height: 350px; font-family: monospace; font-size: 13px;}
        th, td { padding:6px 4px; }
        .tip { background:#f3f6fa; padding:14px 16px; margin:18px 0 22px 0; border-radius:7px; font-size:15px;}
        .client-highlight { background:#ffe7a2; }
    </style>
</head>
<body>
    <h2>Landing Page Analyzer</h2>
    <div class="tip">
        <b>How to Take and Name Screenshots (Manual Mode):</b><br>
        <ul>
            <li><b>Chrome Developer Tools:</b> Right-click, "Inspect" (or press Ctrl+Shift+I), then press Ctrl+Shift+P (Cmd+Shift+P on Mac), type "screenshot", and pick "Capture full size screenshot".</li>
            <li><b>Web Capture (Chrome, Edge):</b> Three dots > More tools > Web capture.</li>
        </ul>
        <b>How to name your screenshot:</b> <br>
        Use the following format: <b><code>[key]_manual.png</code></b><br>
        Example for <code>https://www.udemy.com/course/data-analyst-professional-certificate-in-data-analysis/</code>:<br>
        <code>www_udemy_com_course_data_analyst_professional_certificate_in_data_analysis_manual.png</code><br>
        <br>
        <b>If the site requires login or blocks bots (Cloudflare, Akamai, DataDome, Imperva, or similar anti-bot systems), always use Manual mode and upload your screenshot!</b>
    </div>
    <form method="post" enctype="multipart/form-data">
        <table border="1" cellpadding="0" cellspacing="0">
            <tr>
                <th>#</th>
                <th>URL</th>
                <th>Manual Screenshot?</th>
                <th>Upload Manual Screenshot (.png)</th>
                <th>Client?</th>
            </tr>
            {% for entry in entries %}
            <tr {% if entry.client %}class="client-highlight"{% endif %}>
                <td>{{ loop.index }}</td>
                <td>
                    <input type="text" name="url_{{ loop.index0 }}" value="{{ entry.url }}" style="width:420px">
                </td>
                <td>
                    <input type="checkbox" name="manual_{{ loop.index0 }}" {% if entry.manual %}checked{% endif %}>
                </td>
                <td>
                    <input type="file" name="screenshot_{{ loop.index0 }}">
                </td>
                <td>
                    <input type="radio" name="client_idx" value="{{ loop.index0 }}" {% if entry.client %}checked{% endif %}>
                </td>
            </tr>
            {% endfor %}
        </table>
        <br>
        <button type="button" onclick="addRow()">Add URL</button>
        <button type="submit">Analyze</button>
        <br><br>
        <b>Default Analysis Prompt:</b>
        <textarea name="prompt">{{ prompt }}</textarea>
        <br>
    </form>
    {% if csv_path %}
        <br>
        <a href="/download/csv">Download CSV</a>
        <br><br>
        <form action="/generate_summary" method="post">
            <input type="hidden" name="csv_path" value="{{ csv_path }}">
            <input type="hidden" name="client_name" value="{{ client_name }}">
            <input type="hidden" name="client_lp" value="{{ client_url }}">
            <input type="hidden" name="theme" value="Data Analytics">
            <input type="hidden" name="competitor_urls" value="{{ competitor_urls }}">
            <button type="submit">Generate Strategic Summary & Recommendations</button>
        </form>
    {% endif %}
    <script>
        function addRow() {
            const tbl = document.querySelector("table");
            const rowCount = tbl.rows.length;
            const row = tbl.insertRow(rowCount);
            row.innerHTML = `<td>${rowCount}</td>
                <td><input type="text" name="url_${rowCount-1}" value="" style="width:420px"></td>
                <td><input type="checkbox" name="manual_${rowCount-1}"></td>
                <td><input type="file" name="screenshot_${rowCount-1}"></td>
                <td><input type="radio" name="client_idx" value="${rowCount-1}"></td>`;
        }
    </script>
</body>
</html>
'''

@app.route("/", methods=["GET", "POST"])
def index():
    flushprint("Index route called:", request.method)
    summary = None
    error = None
    csv_path = None
    entries = []
    prompt = default_prompt
    client_url = ""
    client_name = ""
    competitor_urls = ""
    if request.method == "POST":
        prompt = request.form.get("prompt") or default_prompt
        i = 0
        client_idx = request.form.get("client_idx")
        while True:
            url = request.form.get(f"url_{i}")
            if not url:
                break
            url = url.strip()
            key = url_to_key(url)
            manual = request.form.get(f"manual_{i}") == "on"
            is_client = str(i) == str(client_idx)
            entries.append({
                "url": url,
                "key": key,
                "manual": manual,
                "client": is_client
            })
            i += 1

        # Save uploaded manual screenshots
        for idx, entry in enumerate(entries):
            file = request.files.get(f"screenshot_{idx}")
            if file and file.filename:
                filename = f"{entry['key']}_manual.png"
                filepath = os.path.join(UPLOAD_FOLDER, filename)
                file.save(filepath)
                flushprint(f"Saved manual screenshot: {filepath}")

        try:
            results, csv_path = analyze_landing_pages(entries, prompt)
            summary = json.dumps(results, indent=2, ensure_ascii=False)
            # Identify client and competitors for summary
            client_entry = next((e for e in entries if e["client"]), None)
            competitor_entries = [e for e in entries if not e["client"]]
            client_url = client_entry["url"] if client_entry else ""
            client_name = client_entry["key"] if client_entry else ""
            competitor_urls = "\n".join(e["url"] for e in competitor_entries)
        except Exception as e:
            error = str(e)
            csv_path = None
    else:
        # Default 2 rows for user to start with
        entries = [
            {"url": "", "key": "", "manual": False, "client": False},
            {"url": "", "key": "", "manual": False, "client": False},
        ]
        csv_path = None

    return render_template_string(
        HTML,
        prompt=prompt,
        summary=summary,
        error=error,
        csv_path=csv_path,
        entries=entries,
        client_name=client_name,
        client_url=client_url,
        competitor_urls=competitor_urls
    )

@app.route("/download/csv")
def download_csv():
    if os.path.exists(CSV_FILE):
        return send_file(CSV_FILE, as_attachment=True)
    else:
        return "CSV not found."

@app.route("/generate_summary", methods=["POST"])
def generate_summary():
    csv_path = request.form["csv_path"]
    client_name = request.form["client_name"]
    client_lp = request.form["client_lp"]
    theme = request.form.get("theme", "Data Analytics")
    competitor_urls = request.form["competitor_urls"]
    with open(csv_path, "r", encoding="utf-8") as f:
        data_string = f.read()
    prompt = strategy_prompt_template.format(
        client_name=client_name,
        client_lp=client_lp,
        theme=theme,
        competitor_urls=competitor_urls,
        data_string=data_string
    )
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        flushprint("Generating summary via Gemini")
        response = model.generate_content(prompt)
        summary_text = response.text
        return render_template_string("""
            <h2>Strategic Summary & Recommendations</h2>
            <pre style="white-space: pre-wrap;">{{ summary_text }}</pre>
            <a href="/">← Back</a>
        """, summary_text=summary_text)
    except Exception as e:
        return f"Error: {e}"

def analyze_landing_pages(entries, prompt):
    """
    For each entry, analyze the landing page (manual or auto), get Gemini response, build CSV.
    Return (results_dict, csv_path)
    """
    flushprint("analyze_landing_pages called")
    results = []
    header = [
        "Platform", "LP Link", "Main Offer", "Purchase or Lead Gen Form", "Primary CTA",
        "Above the Fold - Headline", "Above the Fold - Trust Elements", "Above the Fold - Other Elements",
        "Above the Fold - Creative (Yes/No)", "Above the Fold - Creative Type", "Above the Fold - Creative Position",
        "Above the Fold - # of CTAs", "Above the Fold - CTA / Form Position", "Primary CTA Just for Free Trial",
        "Secondary CTA", "Clickable Logo", "Navigation Bar"
    ]
    rows = []
    for entry in entries:
        flushprint(f"Processing {entry['key']} ({entry['url']}) manual={entry['manual']}")
        # TODO: Replace with your Gemini/image logic!
        json_result = {
            "Platform": entry['key'],
            "LP Link": entry['url'],
            "Main Offer": "Sample Offer",
            "Purchase or Lead Gen Form": "Lead Generation",
            "Primary CTA": "Enroll Now",
            "Above the Fold - Headline": "Become a Data Analyst",
            "Above the Fold - Trust Elements": "Trusted by 10,000+ learners",
            "Above the Fold - Other Elements": "Free trial available",
            "Above the Fold - Creative (Yes/No)": "Yes",
            "Above the Fold - Creative Type": "Hero image with students",
            "Above the Fold - Creative Position": "Right side",
            "Above the Fold - # of CTAs": "2",
            "Above the Fold - CTA / Form Position": "Top right",
            "Primary CTA Just for Free Trial": "Start Free Trial",
            "Secondary CTA": "View Syllabus",
            "Clickable Logo": "Yes",
            "Navigation Bar": "Yes"
        }
        json_result["Platform"] = entry['key']
        json_result["LP Link"] = entry['url']
        rows.append([json_result.get(h, "") for h in header])
        results.append(json_result)
    with open(CSV_FILE, "w", newline='', encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
    flushprint("CSV saved")
    return results, CSV_FILE

genai.configure(api_key=os.environ.get("GEMINI_API_KEY", ""))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, debug=False)

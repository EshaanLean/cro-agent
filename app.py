import os
import io
import json
import sys
import pandas as pd
from flask import Flask, request, render_template_string, send_file

def flushprint(*args, **kwargs):
    print(*args, **kwargs)
    sys.stdout.flush()

flushprint("=== app.py is starting up ===")

try:
    import google.generativeai as genai
    from PIL import Image
    from playwright.sync_api import sync_playwright
    flushprint("Imports successful")
except Exception as e:
    flushprint("Import error:", e)
    raise

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    flushprint("ERROR: GEMINI_API_KEY env var not set!")
    raise ValueError("GEMINI_API_KEY env var not set! Add it on Render.com dashboard.")
else:
    flushprint("GEMINI_API_KEY loaded")
try:
    genai.configure(api_key=API_KEY)
    flushprint("Gemini configured")
except Exception as e:
    flushprint("Gemini configure failed:", e)
    raise

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = "manual_screenshots"
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
flushprint("UPLOAD_FOLDER checked/created")

# -------- DEFAULT PROMPT ---------
def make_default_prompt(provider_name, url, prompt_text_section):
    return f"""
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

# ------------- HTML TEMPLATE ------------------
HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Landing Page Analyzer (Gemini + Playwright)</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 2em; background: #f6f7fa; }
    .container { max-width: 950px; margin: auto; background: #fff; padding: 2em 2em 1em 2em; border-radius: 12px; box-shadow: 0 8px 24px rgba(30,32,58,.07); }
    .tip { background: #e8f1ff; border-left: 4px solid #4091f7; padding: 1em; margin-bottom: 1.4em; font-size: 1.04em; }
    .urls-table th, .urls-table td { padding: 0.6em 0.5em; }
    .urls-table { width: 100%; border-collapse: collapse; margin-bottom: 1.4em; }
    .urls-table th { background: #f5f6fa; }
    .urls-table td { background: #fcfcfd; }
    .urls-table th, .urls-table td { border-bottom: 1px solid #eaeaea; }
    input[type="text"], textarea { width: 100%; padding: 0.4em; }
    input[type="checkbox"] { transform: scale(1.2); }
    input[type="file"] { width: 100%; }
    button { background: #2266e3; color: #fff; border: none; border-radius: 6px; font-size: 1.1em; padding: 0.6em 1.5em; margin-top: 1.5em; }
    .error { color: #c00; background: #ffe7e7; border-left: 4px solid #f42; padding: 1em; border-radius: 6px; margin-bottom: 1em;}
    .result { background: #f5f5f5; padding: 1em; margin-top: 1.2em; border-radius: 8px; font-size: 1.1em;}
    .add-row { color: #4091f7; background: none; border: none; font-size: 1.25em; cursor: pointer;}
    .remove-row { color: #d66; background: none; border: none; font-size: 1.1em; cursor: pointer;}
    .files-cell { max-width: 180px; }
    .label-small { font-size: 0.96em; color: #555; }
    @media (max-width: 750px) { .container { padding: 1em 0.3em; } }
  </style>
  <script>
    function addRow() {
      var tbl = document.getElementById('urls-table-body');
      var idx = tbl.rows.length;
      var row = tbl.insertRow();
      row.innerHTML = `
        <td><input type="text" name="url_${idx}" placeholder="https://..." required></td>
        <td style="text-align:center;"><input type="checkbox" name="manual_${idx}" onchange="toggleScreenshotInput(this, ${idx})"></td>
        <td class="files-cell"><input type="file" name="screenshot_${idx}" id="screenshot_${idx}" style="display:none" accept="image/png"></td>
        <td><button type="button" class="remove-row" onclick="removeRow(this)">&#10006;</button></td>
      `;
    }
    function removeRow(btn) {
      var row = btn.parentNode.parentNode;
      row.parentNode.removeChild(row);
    }
    function toggleScreenshotInput(checkbox, idx) {
      var inp = document.getElementById('screenshot_' + idx);
      if (checkbox.checked) inp.style.display = 'block';
      else inp.style.display = 'none';
    }
    function toggleAllManuals(source) {
      var checkboxes = document.querySelectorAll('input[type=checkbox][name^="manual_"]');
      for (var i = 0; i < checkboxes.length; i++) {
        checkboxes[i].checked = source.checked;
        checkboxes[i].dispatchEvent(new Event('change'));
      }
    }
    window.onload = function() {
      // Ensure at least one row exists
      if (document.getElementById('urls-table-body').rows.length === 0) {
        addRow();
      }
    }
  </script>
</head>
<body>
<div class="container">
  <h1>Landing Page Analyzer <span style="font-size:0.55em;font-weight:normal;color:#888;">(Gemini + Playwright)</span></h1>

  <div class="tip">
    <b>Instructions & Tips:</b><br>
    - Enter the landing page URLs you want to analyze below.<br>
    - For each URL, if the website <b>requires login, is protected by Cloudflare, bot-blockers, or blocks scraping (e.g., Udemy, Brainstation, Teachable, Thinkific, Kajabi, Shopify admin)</b>, check the "Manual Screenshot" box and upload your PNG screenshot.<br>
    - <b>How to take a screenshot:</b><br>
      <span class="label-small">Chrome: <b>Method 1 (Developer Tools):</b> Open the webpage, right-click, select "Inspect" (or press <b>Ctrl+Shift+I</b>), then press <b>Ctrl+Shift+P</b> (Cmd+Shift+P on Mac), type "screenshot" and select "Capture full size screenshot".<br>
      <b>Method 2 (Web Capture):</b> Click the three dots (⋮) in Chrome's top right, select "More tools" &gt; "Web capture".</span><br>
    - <b>Naming rule:</b> The image filename must be <b>&lt;url-key&gt;_manual.png</b> (the platform key is auto-generated and shown in the table).<br>
    - You can add or remove as many URLs as you need.
  </div>
  <form method="POST" enctype="multipart/form-data">
    <table class="urls-table">
      <thead>
        <tr>
          <th style="width:40%">Landing Page URL</th>
          <th style="width:12%;text-align:center;">
            Manual Screenshot<br>
            <input type="checkbox" onclick="toggleAllManuals(this)" style="margin-top:3px;">
            <div class="label-small">All</div>
          </th>
          <th style="width:30%">Screenshot Upload (PNG)</th>
          <th></th>
        </tr>
      </thead>
      <tbody id="urls-table-body">
        {% for row in entries %}
        <tr>
          <td>
            <input type="text" name="url_{{loop.index0}}" value="{{row.url}}" required>
            <div class="label-small">Platform key: <b>{{row.key}}</b></div>
          </td>
          <td style="text-align:center;">
            <input type="checkbox" name="manual_{{loop.index0}}" {% if row.manual %}checked{% endif %} onchange="toggleScreenshotInput(this, {{loop.index0}})">
          </td>
          <td class="files-cell">
            <input type="file" name="screenshot_{{loop.index0}}" id="screenshot_{{loop.index0}}" style="display:{{'block' if row.manual else 'none'}}" accept="image/png">
          </td>
          <td>
            <button type="button" class="remove-row" onclick="removeRow(this)">&#10006;</button>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    <button type="button" class="add-row" onclick="addRow()">+ Add URL</button>
    <br><br>
    <label for="prompt"><b>Prompt for Gemini:</b></label>
    <textarea name="prompt" rows="16" required>{{prompt or default_prompt}}</textarea>
    <div class="label-small" style="margin-top:-4px;margin-bottom:14px;">
      <b>Edit the prompt as needed. The default covers most competitive landing page analyses.</b>
    </div>
    <button type="submit">Run Analysis</button>
  </form>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  {% if summary %}
    <div class="result">
      <h2>Analysis Summary:</h2>
      <pre>{{summary}}</pre>
      <a href="/download/csv">Download CSV</a>
    </div>
  {% endif %}
</div>
</body>
</html>
"""

# --- Utility: make a unique, reproducible "key" for each URL
def url_to_key(url):
    try:
        url = url.strip()
        if url.endswith("/"):
            url = url[:-1]
        url = url.replace("https://", "").replace("http://", "")
        key = url.replace(".", "_").replace("-", "_").replace("/", "_").replace("?", "_").replace("&", "_").replace("=", "_")
        key = key.lower()
        return key
    except Exception:
        return url

# --- Utility: save manual screenshots and return a dict {key:filename}
def save_manual_screenshots(request, entries):
    saved = {}
    for i, row in enumerate(entries):
        key = row["key"]
        file = request.files.get(f"screenshot_{i}")
        if file and file.filename:
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{key}_manual.png")
            file.save(save_path)
            saved[key] = save_path
    return saved

# --- Gemini multimodal function
def get_multimodal_analysis_from_gemini(page_content, image_bytes, provider_name, url, prompt_override=None):
    flushprint(f"get_multimodal_analysis_from_gemini for {provider_name} at {url}")
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        flushprint("GenAI model created")
        image = Image.open(io.BytesIO(image_bytes))
        flushprint("Image opened for Gemini")
        MAX_PIXELS = 16383
        if image.height > MAX_PIXELS:
            aspect_ratio = image.width / image.height
            new_height = MAX_PIXELS - 1
            new_width = int(new_height * aspect_ratio)
            image = image.resize((new_width, new_height), Image.LANCZOS)
            flushprint("Image resized for Gemini API")
        image_for_api = image
        prompt_text_section = f"""
        - **Text Content (first 15,000 characters):**
        ---
        {page_content[:15000]}
        ---
        """ if page_content else ""
        prompt = prompt_override or make_default_prompt(provider_name, url, prompt_text_section)
        flushprint("Sending prompt to Gemini")
        response = model.generate_content([prompt, image_for_api])
        raw = response.text.strip()
        # Try to find first { ... } json block in response if any text before/after
        import re
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        cleaned_json = match.group(0) if match else raw
        try:
            return json.loads(cleaned_json)
        except Exception as e:
            flushprint("Gemini response not valid JSON:", cleaned_json)
            raise Exception(f"Gemini response was not valid JSON: {cleaned_json}")
    except Exception as e:
        flushprint("Gemini multimodal analysis failed:", e)
        return {"Platform": provider_name, "error": str(e)}

# --- Main analyzer
def analyze_landing_pages(entries, prompt_override=None):
    flushprint("analyze_landing_pages called")
    all_course_data = []
    output_dir = "landing_page_analysis"
    os.makedirs(output_dir, exist_ok=True)
    try:
        with sync_playwright() as p:
            flushprint("Playwright launched")
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36'
            )
            for row in entries:
                provider_name = row["key"]
                url = row["url"]
                flushprint(f"Processing {provider_name} ({url}) manual={row.get('manual')}")
                if row.get("manual"):
                    manual_file = os.path.join(app.config['UPLOAD_FOLDER'], f"{provider_name}_manual.png")
                    if not os.path.exists(manual_file):
                        flushprint(f"Manual screenshot not found: {manual_file}")
                        all_course_data.append({"Platform": provider_name, "error": f"Manual screenshot '{manual_file}' not found."})
                        continue
                    with open(manual_file, "rb") as f:
                        image_bytes = f.read()
                    page_content = ""
                    structured_data = get_multimodal_analysis_from_gemini(page_content, image_bytes, provider_name, url, prompt_override)
                    all_course_data.append(structured_data)
                else:
                    try:
                        flushprint(f"Navigating to {url}")
                        page = context.new_page()
                        page.goto(url, timeout=30000)
                        page_content = page.content()
                        screenshot_bytes = page.screenshot(full_page=True)
                        structured_data = get_multimodal_analysis_from_gemini(page_content, screenshot_bytes, provider_name, url, prompt_override)
                        all_course_data.append(structured_data)
                        page.close()
                    except Exception as e:
                        flushprint(f"Auto-screenshot failed for {provider_name}: {e}")
                        all_course_data.append({"Platform": provider_name, "error": f"Auto-screenshot failed: {str(e)}"})
            browser.close()
            flushprint("Browser closed")
    except Exception as e:
        flushprint("Playwright failed:", e)
        for row in entries:
            all_course_data.append({"Platform": row["key"], "error": f"Playwright failed: {e}"})
    # Save results
    df = pd.DataFrame(all_course_data)
    csv_path = os.path.join(output_dir, "competitive_analysis_results.csv")
    df.to_csv(csv_path, index=False)
    flushprint("CSV saved")
    return all_course_data, csv_path

# ----------- FLASK ROUTES -----------
@app.route("/", methods=["GET", "POST"])
def index():
    flushprint("Index route called:", request.method)
    error, summary, csv_path = None, None, None
    default_prompt = make_default_prompt("{{provider_name}}", "{{url}}", "{{prompt_text_section}}")
    # Handle form data (parse by index)
    entries = []
    if request.method == "POST":
        prompt = request.form.get("prompt") or default_prompt
        # Reconstruct entries by row
        i = 0
        while True:
            url = request.form.get(f"url_{i}")
            if not url:
                break
            url = url.strip()
            key = url_to_key(url)
            manual = request.form.get(f"manual_{i}") == "on"
            entries.append({
                "url": url,
                "key": key,
                "manual": manual
            })
            i += 1
        if not entries:
            error = "Enter at least one URL."
            return render_template_string(HTML, error=error, entries=[], prompt=prompt, default_prompt=default_prompt)
        # Save manual screenshots (if any)
        save_manual_screenshots(request, entries)
        try:
            results, csv_path = analyze_landing_pages(entries, prompt)
            summary = json.dumps(results, indent=2, ensure_ascii=False)
        except Exception as e:
            error = f"Error during analysis: {e}"
            flushprint(error)
            summary = None
    else:
        prompt = default_prompt
        entries = []
    return render_template_string(HTML, error=error, summary=summary, csv_path=csv_path, entries=entries, prompt=prompt, default_prompt=default_prompt)

@app.route("/download/csv")
def download_csv():
    file_path = "landing_page_analysis/competitive_analysis_results.csv"
    if not os.path.exists(file_path):
        return "No CSV available.", 404
    return send_file(file_path, as_attachment=True)

if __name__ == "__main__":
    app.run(port=10000, host="0.0.0.0")

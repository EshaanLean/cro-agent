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
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
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

# The default prompt (Jinja will inject provider_name, url, etc. dynamically)
RAW_DEFAULT_PROMPT = """
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

HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Landing Page Analyzer (Gemini + Playwright)</title>
  <style>
    body { font-family: Inter, Arial, sans-serif; background: #f7faff; margin: 0; }
    .container { max-width: 850px; margin: 32px auto 0; background: #fff; border-radius: 16px; box-shadow: 0 2px 16px #0001; padding: 32px 28px 32px 28px; }
    h1 { margin-top: 0; }
    .tip, .tip-list { background: #e6f7ff; color: #004466; border-radius: 8px; padding: 18px 20px; margin-bottom: 26px; font-size: 1.07em; }
    .tip-list ul { margin-top: 0; margin-bottom: 0; }
    label { font-weight: 600; margin-bottom: 4px; display: block; }
    textarea, input[type=text] { width: 100%; font-size: 1.06em; padding: 10px; margin-bottom: 14px; border-radius: 6px; border: 1px solid #c5d1db; }
    textarea { min-height: 92px; }
    select, input[type=file] { font-size: 1.06em; margin-right: 8px; }
    .url-row { display: flex; gap: 7px; margin-bottom: 12px; align-items: center; }
    .url-row input[type=text] { flex: 2.5; min-width: 210px; }
    .url-row select { flex: 0.8; min-width: 74px; }
    .url-row input[type=file] { flex: 1.5; }
    .url-row .remove-btn { flex: 0 0 auto; background: #eee; border: none; border-radius: 6px; padding: 5px 15px; margin-left: 3px; cursor: pointer; }
    .url-row .remove-btn:hover { background: #e57373; color: white; }
    .add-btn { margin: 10px 0 28px 0; background: #1976d2; color: #fff; border: none; border-radius: 7px; padding: 7px 19px; cursor: pointer; font-size: 1em; }
    .add-btn:hover { background: #125b9c; }
    .result { background: #f5f5f5; padding: 1em; margin-top: 1em; border-radius: 8px; }
    .error { color: #c62828; font-weight: 500; margin: 18px 0; }
    .download-link { display: inline-block; margin-top: 16px; font-weight: 600; color: #0e5386; }
    .hide { display: none !important; }
    @media (max-width: 670px) {
      .container { padding: 15px; }
      .url-row { flex-direction: column; gap: 2px; }
      .url-row input[type=text], .url-row select, .url-row input[type=file], .url-row .remove-btn { width: 100%; margin: 3px 0; }
    }
  </style>
  <script>
    function toggleManual(selectElem, idx) {
      var fileInput = document.getElementsByClassName('screenshot-input')[idx];
      if (selectElem.value === "manual") {
        fileInput.classList.remove('hide');
      } else {
        fileInput.classList.add('hide');
      }
    }
    function addRow() {
      const urlList = document.getElementById('url-list');
      const n = urlList.children.length;
      let div = document.createElement('div');
      div.className = 'url-row';
      div.innerHTML = `
        <input type="text" name="urls" placeholder="Paste landing page URL here..." required>
        <select name="modes" onchange="toggleManual(this, ${n})">
          <option value="auto" selected>Auto</option>
          <option value="manual">Manual</option>
        </select>
        <input type="file" name="screenshots" accept="image/png" class="screenshot-input hide"/>
        <button type="button" class="remove-btn" onclick="removeRow(this)">Remove</button>
      `;
      urlList.appendChild(div);
    }
    function removeRow(btn) {
      btn.parentElement.remove();
    }
    window.onload = function() {
      let selects = document.getElementsByName("modes");
      let fileInputs = document.getElementsByClassName("screenshot-input");
      for (let i = 0; i < selects.length; ++i) {
        selects[i].onchange = function() { toggleManual(this, i); };
        if (selects[i].value !== "manual") {
          fileInputs[i].classList.add('hide');
        }
      }
    }
  </script>
</head>
<body>
<div class="container">
  <h1>Landing Page Analyzer (Gemini + Playwright)</h1>

  <div class="tip-list">
    <ul>
      <li><b>Step 1:</b> For most websites, keep mode as <b>Auto</b>. For pages that <b>block bots</b> (Cloudflare, hCaptcha, Sucuri, PerimeterX, sites requiring login like Udemy, Brainstation, Coursera, etc), switch mode to <b>Manual</b> and upload your screenshot.</li>
      <li><b>Step 2:</b> <b>How to take a full-page screenshot in Chrome:</b><br>
        <b>Method 1:</b> Open the webpage, right-click, select "Inspect" (or press Ctrl+Shift+I), then press Ctrl+Shift+P (or Cmd+Shift+P on Mac), type "screenshot", choose <b>"Capture full size screenshot"</b>.<br>
        <b>Method 2:</b> Click the three dots (top-right in Chrome), select "More tools" &gt; "Web capture" and capture full page.<br>
        <b>Naming:</b> Name your image <b>&lt;provider&gt;_manual.png</b> (e.g. <b>udemy_manual.png</b>).
      </li>
    </ul>
  </div>

  <form method="POST" enctype="multipart/form-data">
    <label>Landing Page URLs & Modes:</label>
    <div id="url-list">
      {% for entry in entries %}
      <div class="url-row">
        <input type="text" name="urls" value="{{ entry.url }}" placeholder="Paste landing page URL here..." required>
        <select name="modes" onchange="toggleManual(this, {{ loop.index0 }})">
          <option value="auto" {% if not entry.manual %}selected{% endif %}>Auto</option>
          <option value="manual" {% if entry.manual %}selected{% endif %}>Manual</option>
        </select>
        <input type="file" name="screenshots" accept="image/png" class="screenshot-input {% if not entry.manual %}hide{% endif %}" />
        {% if not loop.first %}
        <button type="button" class="remove-btn" onclick="removeRow(this)">Remove</button>
        {% endif %}
      </div>
      {% endfor %}
    </div>
    <button type="button" class="add-btn" onclick="addRow()">+ Add Another URL</button>
    <label>Gemini Analysis Prompt (edit as needed):</label>
    <textarea name="prompt" rows="16">{{ prompt or default_prompt }}</textarea>
    <button type="submit" style="margin-top:18px;">Run Analysis</button>
  </form>

  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  {% if summary %}
    <div class="result">
      <h2>Analysis Summary:</h2>
      <pre>{{summary}}</pre>
      <a class="download-link" href="/download/csv">Download CSV</a>
    </div>
  {% endif %}
</div>
</body>
</html>
"""

def save_manual_screenshots(files):
    uploaded_names = []
    for file in files.getlist("screenshots"):
        if file and file.filename:
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
            file.save(save_path)
            uploaded_names.append(file.filename)
    return uploaded_names

def get_multimodal_analysis_from_gemini(page_content, image_bytes, provider_name, url, prompt_override=None):
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        image = Image.open(io.BytesIO(image_bytes))
        MAX_PIXELS = 16383
        if image.height > MAX_PIXELS:
            aspect_ratio = image.width / image.height
            new_height = MAX_PIXELS - 1
            new_width = int(new_height * aspect_ratio)
            image = image.resize((new_width, new_height), Image.LANCZOS)
        image_for_api = image
        prompt_text_section = f"""
        - **Text Content (first 15,000 characters):**
        ---
        {page_content[:15000]}
        ---
        """ if page_content else ""
        prompt = (prompt_override or RAW_DEFAULT_PROMPT).format(
            provider_name=provider_name,
            url=url,
            prompt_text_section=prompt_text_section
        )
        response = model.generate_content([prompt, image_for_api])
        cleaned = response.text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        cleaned = cleaned.strip().rstrip("```").strip()
        try:
            return json.loads(cleaned)
        except Exception:
            raise Exception("Gemini response was not valid JSON: " + cleaned[:500])
    except Exception as e:
        raise

def analyze_landing_pages(landing_pages, prompt_override=None):
    all_course_data = []
    output_dir = "landing_page_analysis"
    os.makedirs(output_dir, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36'
            )
            for lp in landing_pages:
                if lp.get("manual", False):
                    manual_file = f"{lp['name']}_manual.png"
                    manual_path = os.path.join(app.config['UPLOAD_FOLDER'], manual_file)
                    if not os.path.exists(manual_path):
                        all_course_data.append({"Platform": lp['name'], "error": f"Manual screenshot '{manual_file}' not found."})
                        continue
                    with open(manual_path, "rb") as f:
                        image_bytes = f.read()
                    page_content = ""
                    try:
                        structured_data = get_multimodal_analysis_from_gemini(page_content, image_bytes, lp['name'], lp['url'], prompt_override)
                        all_course_data.append(structured_data)
                    except Exception as e:
                        all_course_data.append({"Platform": lp['name'], "error": str(e)})
                else:
                    page = context.new_page()
                    try:
                        page.goto(lp["url"], wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(5000)
                        screenshot_path = os.path.join(output_dir, f"{lp['name']}_fullpage.png")
                        page.screenshot(path=screenshot_path, full_page=True)
                        with open(screenshot_path, "rb") as f:
                            image_bytes = f.read()
                        page_content = page.inner_text('body')
                        structured_data = get_multimodal_analysis_from_gemini(page_content, image_bytes, lp['name'], lp['url'], prompt_override)
                        all_course_data.append(structured_data)
                    except Exception as e:
                        all_course_data.append({"Platform": lp['name'], "error": str(e)})
                    finally:
                        page.close()
            browser.close()
    except Exception as e:
        raise

    df = pd.DataFrame(all_course_data)
    df.to_csv(os.path.join(output_dir, "competitive_analysis_data.csv"), index=False)
    summary = df.to_string(index=False)
    return summary, os.path.join(output_dir, "competitive_analysis_data.csv")

@app.route("/", methods=["GET", "POST"])
def index():
    summary = None
    error = None
    prompt = ''
    entries = []

    if request.method == "POST":
        urls = request.form.getlist("urls")
        modes = request.form.getlist("modes")
        prompt = request.form.get("prompt") or RAW_DEFAULT_PROMPT.strip()
        uploaded_files = request.files
        save_manual_screenshots(uploaded_files)
        landing_pages = []
        entries = []
        for i, url in enumerate(urls):
            url = url.strip()
            if not url:
                continue
            mode = modes[i] if i < len(modes) else "auto"
            base_name = url.split("//")[-1].split("/")[1] if "/" in url.split("//")[-1] else url.split("//")[-1]
            name = base_name.lower().replace(".", "_").replace("-", "_")
            is_manual = (mode == "manual")
            landing_pages.append({
                "name": name,
                "url": url,
                "manual": is_manual
            })
            entries.append({
                "url": url,
                "manual": is_manual
            })
        try:
            summary, csv_path = analyze_landing_pages(landing_pages, prompt)
        except Exception as e:
            error = str(e)
    else:
        entries = [{"url": "", "manual": False}]

    return render_template_string(
        HTML,
        summary=summary,
        error=error,
        prompt=prompt,
        default_prompt=RAW_DEFAULT_PROMPT.strip(),
        entries=entries
    )

@app.route('/download/csv')
def download_csv():
    path = "landing_page_analysis/competitive_analysis_data.csv"
    if not os.path.exists(path):
        return "No file available", 404
    return send_file(path, as_attachment=True, download_name="competitive_analysis_data.csv")

@app.route("/ping")
def ping():
    return "pong"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flushprint(f"Starting Flask app on port {port}")
    app.run(host="0.0.0.0", port=port)

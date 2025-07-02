import os
import io
import json
import sys
import pandas as pd
from flask import Flask, request, render_template_string, send_file
from urllib.parse import urlparse

# --- Flushed print utility ---
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

def url_to_key(url):
    p = urlparse(url)
    domain = p.netloc.replace("www.", "")
    path = "_".join([s for s in p.path.strip("/").split("/") if s])
    key = f"{domain}_{path}" if path else domain
    return key.replace(".", "_").replace("-", "_")

# ---- TIPS to show on UI ----
SCREENSHOT_TIPS = """
<b>How to take a full-page screenshot (Chrome):</b><br>
1. <b>Developer Tools:</b> Open the webpage, right-click, select <b>Inspect</b> (or press <b>Ctrl+Shift+I</b>), then press <b>Ctrl+Shift+P</b> (Cmd+Shift+P on Mac) to open the command palette. Type "screenshot" and choose the desired option (e.g., "Capture full size screenshot").<br>
2. <b>Web Capture:</b> Click the three dots (Settings & more) in the top-right, select <b>More tools</b> &gt; <b>Web capture</b>. Choose full page.<br>
<b>File Upload Tips:</b><br>
- You don't need to rename your screenshot! Just select the screenshot file and the app will auto-match it to the correct URL.<br>
- If the site <b>requires login</b> or <b>blocks bots</b> (examples: Udemy, Brainstation, Cloudflare-protected, some Coursera, CXL, etc.), <b>always upload your own screenshot</b>.<br>
"""

# ---- Default Prompt (huge, all fields, for direct editing) ----
def get_default_prompt(provider_name, url, prompt_text_section):
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

# -- Save uploaded screenshots and rename to url-key --
def save_manual_screenshots(files, url_key_map):
    uploaded_keys = set()
    for file in files.getlist("screenshots"):
        if file.filename:
            orig_name = file.filename
            # Try to match file to a url by file name, fallback: just assign in order
            matched_key = None
            # Try to match by original filename containing a url-key
            for key in url_key_map.keys():
                if key in orig_name:
                    matched_key = key
                    break
            # If no match, assign to first unassigned key (ordered)
            if not matched_key:
                for key in url_key_map.keys():
                    if key not in uploaded_keys:
                        matched_key = key
                        break
            if not matched_key:
                continue
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{matched_key}_manual.png")
            file.save(save_path)
            uploaded_keys.add(matched_key)
            flushprint(f"Saved manual screenshot: {save_path}")
    return uploaded_keys

def get_multimodal_analysis_from_gemini(page_content: str, image_bytes: bytes, provider_name: str, url: str, prompt_override=None):
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

        prompt = prompt_override or get_default_prompt(provider_name, url, prompt_text_section)

        flushprint("Sending prompt to Gemini")
        response = model.generate_content([prompt, image_for_api])
        cleaned_json = response.text.strip().replace("```json", "").replace("```", "")
        flushprint("Gemini responded. JSON parsed.")
        return json.loads(cleaned_json)
    except Exception as e:
        flushprint("Gemini multimodal analysis failed:", e)
        raise

def analyze_landing_pages(landing_pages, prompt_override_dict=None):
    flushprint("analyze_landing_pages called")
    all_course_data = []
    output_dir = "landing_page_analysis"
    os.makedirs(output_dir, exist_ok=True)
    try:
        with sync_playwright() as p:
            flushprint("Playwright launched")
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36'
            )
            for lp in landing_pages:
                flushprint(f"Processing {lp['name']} ({lp['url']}) manual={lp.get('manual')}")
                provider_name = lp['name']
                url = lp['url']
                prompt = prompt_override_dict.get(provider_name) if prompt_override_dict else None
                prompt_text_section = ""
                if lp.get("manual", False):
                    manual_file = f"{provider_name}_manual.png"
                    manual_path = os.path.join(app.config['UPLOAD_FOLDER'], manual_file)
                    if not os.path.exists(manual_path):
                        flushprint(f"Manual screenshot not found: {manual_file}")
                        all_course_data.append({"Platform": provider_name, "error": f"Manual screenshot '{manual_file}' not found."})
                        continue
                    with open(manual_path, "rb") as f:
                        image_bytes = f.read()
                    page_content = ""
                    try:
                        structured_data = get_multimodal_analysis_from_gemini(page_content, image_bytes, provider_name, url, prompt)
                        all_course_data.append(structured_data)
                        flushprint(f"Gemini result added for {provider_name}")
                    except Exception as e:
                        flushprint(f"Error for manual {provider_name}:", e)
                        all_course_data.append({"Platform": provider_name, "error": str(e)})
                else:
                    page = context.new_page()
                    try:
                        flushprint(f"Navigating to {url}")
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(5000)
                        screenshot_path = os.path.join(output_dir, f"{provider_name}_fullpage.png")
                        page.screenshot(path=screenshot_path, full_page=True)
                        with open(screenshot_path, "rb") as f:
                            image_bytes = f.read()
                        page_content = page.inner_text('body')
                        structured_data = get_multimodal_analysis_from_gemini(page_content, image_bytes, provider_name, url, prompt)
                        all_course_data.append(structured_data)
                        flushprint(f"Playwright + Gemini result added for {provider_name}")
                    except Exception as e:
                        flushprint(f"Error for auto {provider_name}:", e)
                        all_course_data.append({"Platform": provider_name, "error": str(e)})
                    finally:
                        page.close()
            browser.close()
            flushprint("Browser closed")
    except Exception as e:
        flushprint("Fatal error in analyze_landing_pages:", e)
        raise

    df = pd.DataFrame(all_course_data)
    df.to_csv(os.path.join(output_dir, "competitive_analysis_data.csv"), index=False)
    flushprint("CSV saved")
    summary = df.to_string(index=False)
    return summary, os.path.join(output_dir, "competitive_analysis_data.csv")

# -- HTML (with table, toggle, and tips) --
HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Landing Page Analyzer (Gemini + Playwright)</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 2em; background: #f7faff;}
    .container { max-width: 840px; margin: auto; background: #fff; border-radius: 12px; box-shadow: 0 3px 18px #0001; padding: 32px 28px 32px 28px;}
    h1 { font-size: 2.1em; }
    label { font-weight: bold; margin-top: 10px; display:block;}
    .tips { background: #e6f0fa; border-left: 5px solid #8bc3ff; padding: 1em; margin-bottom: 2em; border-radius: 10px; font-size:1em;}
    textarea, input[type=text] { width: 100%; font-family: monospace; margin-top: 4px; }
    textarea { font-size: 1em; }
    table.urls { width: 100%; margin-bottom: 12px; border-collapse: collapse; background: #fafdff; }
    .manual-toggle { width: 80px; text-align:center;}
    .file-upload { margin-bottom: 1em; }
    .result { background: #f5f5f5; padding: 1em; margin-top: 1em; border-radius: 8px; }
    .error { color: red; margin-top: 1em; }
    th, td { padding: 8px; border-bottom: 1px solid #d4e4f7;}
    th {background: #f1f9ff;}
    .btn { font-size: 1em; background: #2b70e0; color: #fff; border: none; padding: 12px 24px; border-radius: 7px; cursor: pointer; }
    .btn:hover { background: #2160be;}
    .btn:disabled { background: #bbb;}
  </style>
  <script>
    // Add/remove URL rows, toggle manual, filename hint
    function addRow() {
      const t = document.getElementById('urltable');
      let row = t.insertRow(-1);
      row.innerHTML = '<td><input name="urls" type="text" required style="width:98%"></td><td class="manual-toggle"><input name="manuals" type="checkbox"></td>';
    }
    function removeRow() {
      const t = document.getElementById('urltable');
      if(t.rows.length>1) t.deleteRow(-1);
    }
  </script>
</head>
<body>
<div class="container">
  <h1>Landing Page Analyzer (Gemini + Playwright)</h1>
  <div class="tips">{{ tips|safe }}</div>
  <form method="POST" enctype="multipart/form-data">
    <label>Landing Page URLs (each on a separate row):</label>
    <table class="urls" id="urltable">
      <tr>
        <th>Landing Page URL</th>
        <th class="manual-toggle">Manual Screenshot?</th>
      </tr>
      {% for row in entries %}
      <tr>
        <td>
          <input name="urls" type="text" required value="{{ row['url'] }}" style="width:98%">
        </td>
        <td class="manual-toggle">
          <input name="manuals" type="checkbox" {% if row['manual'] %}checked{% endif %}>
        </td>
      </tr>
      {% endfor %}
      {% if not entries %}
      <tr>
        <td><input name="urls" type="text" required></td>
        <td class="manual-toggle"><input name="manuals" type="checkbox"></td>
      </tr>
      {% endif %}
    </table>
    <button type="button" onclick="addRow()">+ Add URL</button>
    <button type="button" onclick="removeRow()">- Remove</button>
    <br><br>
    <label>Upload screenshots (for all manual URLs):</label>
    <div class="file-upload">
      <input type="file" name="screenshots" multiple>
      <small>For protected/login/bot-blocked sites (Cloudflare, Udemy, CXL, Coursera, Brainstation, etc), upload a PNG screenshot. You don't need to rename; we'll auto-match.</small>
    </div>
    <label>Prompt for Gemini (edit if needed):</label>
    <textarea name="prompt" rows="20">{{ default_prompt }}</textarea><br><br>
    <button class="btn" type="submit">Run Analysis</button>
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

@app.route("/", methods=["GET", "POST"])
def index():
    flushprint("Index route called:", request.method)
    summary = None
    error = None
    csv_path = None

    # --- Default: one empty entry for UI, or from POST ---
    entries = []
    if request.method == "POST":
        urls = request.form.getlist("urls")
        manuals = request.form.getlist("manuals")
        # If user didn't check the box, manuals[] will be shorter, so:
        manual_flags = []
        for idx in range(len(urls)):
            try:
                manual_flags.append(request.form.getlist("manuals")[idx] == "on")
            except:
                manual_flags.append(False)
        entries = [{"url": urls[i], "manual": manual_flags[i] if i < len(manual_flags) else False} for i in range(len(urls))]
    else:
        entries = [{"url": "", "manual": False}]
    
    # --- Prepare url_key_map for all entries (so we can match uploads) ---
    url_key_map = {}
    for row in entries:
        if row["url"].strip():
            url_key_map[url_to_key(row["url"].strip())] = row["url"].strip()
    flushprint("url_key_map:", url_key_map)

    default_prompt = get_default_prompt("{provider_name}", "{url}", "{prompt_text_section}")

    if request.method == "POST":
        # --- Save screenshots, mapped to correct url_key ---
        save_manual_screenshots(request.files, url_key_map)

        # --- Build landing_pages dicts for processing ---
        landing_pages = []
        for row in entries:
            if row["url"].strip():
                key = url_to_key(row["url"].strip())
                manual_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{key}_manual.png")
                landing_pages.append({
                    "name": key,
                    "url": row["url"].strip(),
                    "manual": row["manual"] or os.path.exists(manual_path)
                })
        flushprint("Landing pages dict:", landing_pages)
        try:
            summary, csv_path = analyze_landing_pages(landing_pages)
            flushprint("Analysis summary done.")
        except Exception as e:
            error = str(e)
            flushprint("Error in POST analyze_landing_pages:", e)

    return render_template_string(
        HTML,
        summary=summary,
        error=error,
        tips=SCREENSHOT_TIPS,
        default_prompt=default_prompt,
        entries=entries
    )

@app.route('/download/csv')
def download_csv():
    flushprint("Download CSV requested")
    path = "landing_page_analysis/competitive_analysis_data.csv"
    if not os.path.exists(path):
        flushprint("CSV not found")
        return "No file available", 404
    flushprint("CSV found, sending")
    return send_file(path, as_attachment=True, download_name="competitive_analysis_data.csv")

@app.route("/ping")
def ping():
    flushprint("pinged /ping endpoint")
    return "pong"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flushprint(f"Starting Flask app on port {port}")
    app.run(host="0.0.0.0", port=port)

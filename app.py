import os
import io
import json
import sys
import pandas as pd
from flask import Flask, request, render_template_string, send_file

def flushprint(*args, **kwargs):
    print(*args, **kwargs)
    sys.stdout.flush()

def build_default_prompt(provider_name, url, prompt_text_section=""):
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

HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Landing Page Analyzer (Gemini + Playwright)</title>
  <style>
    body { font-family: Arial; margin: 2em; background: #fafaff; }
    .container { max-width: 830px; margin: auto; background: #fff; border-radius: 8px; box-shadow: 0 4px 18px #ececec; padding: 2em 2em 1em 2em;}
    textarea, input[type=text] { width: 100%; font-size: 1em; }
    .file-upload { margin-bottom: 1em; }
    label { font-weight: bold; }
    .result { background: #f5f7fc; padding: 1em; margin-top: 1em; border-radius: 8px; }
    .error { color: red; margin: 1em 0; }
    .tip { color: #444; font-size: 0.98em; background: #eaf3ff; padding: 10px 16px; border-radius: 7px; margin: 18px 0 12px 0;}
    .tips-list {margin: 0.2em 0 1.3em 0; padding-left: 1.2em;}
    code { background: #eef3f7; padding: 0.1em 0.35em; border-radius: 5px;}
    h1 {margin-top: 0.3em;}
    .url-group {background: #f4f7fa; border-radius: 6px; padding: 1em 1em 0.5em 1em; margin-bottom: 1em;}
    .manual-toggle {margin-bottom: 0.5em;}
    .manual-upload {display: none;}
  </style>
  <script>
    function updateUrlFields() {
      let urlBox = document.getElementById('urls');
      let urlList = urlBox.value.split('\\n').filter(u => u.trim().length > 0);
      let urlFieldsDiv = document.getElementById('url-fields');
      urlFieldsDiv.innerHTML = "";
      for (let i = 0; i < urlList.length; i++) {
        let url = urlList[i].trim();
        let html = `
        <div class="url-group" id="urlgroup${i}">
          <b>URL:</b> <span>${url}</span>
          <div class="manual-toggle">
            <label>
              <input type="radio" name="mode_${i}" value="auto" checked onchange="toggleManualUpload(${i})"> Auto-capture
            </label>
            &nbsp;
            <label>
              <input type="radio" name="mode_${i}" value="manual" onchange="toggleManualUpload(${i})"> Manual Screenshot
            </label>
          </div>
          <div class="file-upload manual-upload" id="manual-upload-${i}">
            <input type="file" name="screenshots_${i}">
            <small>Upload a PNG screenshot named <code>&lt;name&gt;_manual.png</code></small>
          </div>
        </div>
        `;
        urlFieldsDiv.innerHTML += html;
      }
    }
    function toggleManualUpload(idx) {
      let isManual = document.querySelector(`input[name="mode_${idx}"][value="manual"]`).checked;
      document.getElementById('manual-upload-' + idx).style.display = isManual ? "block" : "none";
    }
    window.onload = function() { updateUrlFields(); }
  </script>
</head>
<body>
<div class="container">
  <h1>Landing Page Analyzer (Gemini + Playwright)</h1>
  <div class="tip">
    <b>How to take a screenshot in Chrome:</b>
    <ul class="tips-list">
      <li>
        <b>Method 1 (using Developer Tools):</b> Open the webpage, right-click and select "Inspect" (or press <b>Ctrl+Shift+I</b>), then press <b>Ctrl+Shift+P</b> (Cmd+Shift+P on Mac) to open the command palette. Type <b>screenshot</b> and choose the desired option (e.g. <i>Capture full size screenshot</i>).
      </li>
      <li>
        <b>Method 2 (Web Capture):</b> Click the three dots (<b>⋮</b>) in the top-right of Chrome, select <b>More tools</b>, then <b>Web capture</b>. You can select an area or capture the full page.
      </li>
      <li>
        <b>Naming convention:</b> Save your screenshot as <code>&lt;name&gt;_manual.png</code> (e.g., <code>udemy_manual.png</code>).
      </li>
      <li>
        For best results: include only the visible ("above the fold") section, unless you want the whole page.
      </li>
    </ul>
    <b>When do I need a manual screenshot?</b>
    <ul class="tips-list">
      <li>
        If the site <b>requires login</b> or <b>blocks bots</b> (e.g., Cloudflare, DataDome, PerimeterX, Akamai Bot Manager, or protected platforms like Udemy, Brainstation, Coursera), upload your own screenshot!
      </li>
    </ul>
  </div>
  <form method="POST" enctype="multipart/form-data">
    <label>Landing Page URLs (one per line):</label>
    <textarea name="urls" id="urls" rows="4" required oninput="updateUrlFields();">{{urls or ""}}</textarea>
    <div id="url-fields"></div>
    <label>Prompt for Gemini (editable):</label>
    <textarea name="prompt" rows="18" required>{{prompt or default_prompt_text}}</textarea>
    <div style="font-size: 0.95em; color: #888; margin-bottom: 1.5em;">
      <b>Edit the prompt as needed above, or use as-is.</b>
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

def get_url_name(url):
    base = url.split("//")[-1]
    base_name = base.split("/")[1] if "/" in base else base
    return base_name.lower().replace(".", "_").replace("-", "_")

def save_manual_screenshots(files, url_modes, url_names):
    uploaded = []
    for idx, mode in enumerate(url_modes):
        if mode == "manual":
            field = f"screenshots_{idx}"
            file = files.get(field)
            if file and file.filename:
                save_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
                file.save(save_path)
                uploaded.append(file.filename)
    return uploaded

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

        prompt = prompt_override or build_default_prompt(provider_name, url, prompt_text_section)
        flushprint("Sending prompt to Gemini")
        response = model.generate_content([prompt, image_for_api])

        cleaned_json = response.text.strip().replace("```json", "").replace("```", "")
        try:
            return json.loads(cleaned_json)
        except Exception:
            flushprint("Gemini response was:\n", response.text)
            raise ValueError("Gemini response was not valid JSON: " + str(response.text[:400]))
    except Exception as e:
        flushprint("Gemini multimodal analysis failed:", e)
        raise

def analyze_landing_pages(landing_pages, prompt_override=None):
    flushprint("analyze_landing_pages called")
    all_data = []
    output_dir = "landing_page_analysis"
    os.makedirs(output_dir, exist_ok=True)
    flushprint(f"Output dir ready: {output_dir}")

    try:
        with sync_playwright() as p:
            flushprint("Playwright launched")
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 1920, 'height': 1080},
                                         user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36')

            for lp in landing_pages:
                flushprint(f"Processing {lp['name']} ({lp['url']}) manual={lp.get('manual')}")
                if lp.get("manual", False):
                    manual_file = f"{lp['name']}_manual.png"
                    manual_path = os.path.join(app.config['UPLOAD_FOLDER'], manual_file)
                    if not os.path.exists(manual_path):
                        flushprint(f"Manual screenshot not found: {manual_file}")
                        all_data.append({"Platform": lp['name'], "error": f"Manual screenshot '{manual_file}' not found."})
                        continue
                    with open(manual_path, "rb") as f:
                        image_bytes = f.read()
                    page_content = ""
                    try:
                        structured = get_multimodal_analysis_from_gemini(page_content, image_bytes, lp['name'], lp['url'], prompt_override)
                        all_data.append(structured)
                        flushprint(f"Gemini result added for {lp['name']}")
                    except Exception as e:
                        flushprint(f"Error for manual {lp['name']}:", e)
                        all_data.append({"Platform": lp['name'], "error": str(e)})
                else:
                    page = context.new_page()
                    try:
                        flushprint(f"Navigating to {lp['url']}")
                        page.goto(lp["url"], wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(5000)
                        screenshot_path = os.path.join(output_dir, f"{lp['name']}_fullpage.png")
                        page.screenshot(path=screenshot_path, full_page=True)
                        with open(screenshot_path, "rb") as f:
                            image_bytes = f.read()
                        page_content = page.inner_text('body')
                        structured = get_multimodal_analysis_from_gemini(page_content, image_bytes, lp['name'], lp['url'], prompt_override)
                        all_data.append(structured)
                        flushprint(f"Playwright + Gemini result added for {lp['name']}")
                    except Exception as e:
                        flushprint(f"Error for auto {lp['name']}:", e)
                        all_data.append({"Platform": lp['name'], "error": str(e)})
                    finally:
                        page.close()
            browser.close()
            flushprint("Browser closed")
    except Exception as e:
        flushprint("Fatal error in analyze_landing_pages:", e)
        raise

    df = pd.DataFrame(all_data)
    df.to_csv(os.path.join(output_dir, "competitive_analysis_data.csv"), index=False)
    flushprint("CSV saved")
    summary = df.to_string(index=False)
    return summary, os.path.join(output_dir, "competitive_analysis_data.csv")

@app.route("/", methods=["GET", "POST"])
def index():
    flushprint("Index route called:", request.method)
    summary = None
    error = None
    prompt = ''
    urls = ''
    csv_path = None

    default_prompt_text = build_default_prompt("provider_name", "https://example.com")

    if request.method == "POST":
        flushprint("POST data:", request.form)
        urls = request.form.get("urls")
        prompt = request.form.get("prompt")
        uploaded_files = request.files

        url_list = [u.strip() for u in (urls or "").splitlines() if u.strip()]
        url_modes = []
        url_names = []
        for idx, url in enumerate(url_list):
            mode = request.form.get(f"mode_{idx}", "auto")
            url_modes.append(mode)
            url_names.append(get_url_name(url))

        save_manual_screenshots(uploaded_files, url_modes, url_names)
        flushprint("Manual screenshots saved (if any)")

        landing_pages = []
        for i, url in enumerate(url_list):
            name = url_names[i]
            landing_pages.append({
                "name": name,
                "url": url,
                "manual": (url_modes[i] == "manual")
            })
        flushprint("Landing pages dict:", landing_pages)
        try:
            summary, csv_path = analyze_landing_pages(landing_pages, prompt if prompt else None)
            flushprint("Analysis summary done.")
        except Exception as e:
            error = str(e)
            flushprint("Error in POST analyze_landing_pages:", e)

    return render_template_string(HTML, summary=summary, error=error, urls=urls, prompt=prompt, default_prompt_text=default_prompt_text)

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

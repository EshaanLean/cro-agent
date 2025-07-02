import os
import io
import json
import sys
import pandas as pd
from flask import Flask, request, render_template_string, send_file

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

# --- Default Prompt Template (with placeholders) ---
DEFAULT_PROMPT_TEMPLATE = """
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

# --- UI: Dynamic, per-URL manual/auto toggle and file upload ---
HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Landing Page Analyzer (Gemini + Playwright)</title>
  <style>
    body { font-family: Arial, background: #f7f9fa; margin: 0; padding: 0;}
    .container { max-width: 820px; margin: 40px auto; background: #fff; padding: 2em 2.5em; border-radius: 14px; box-shadow: 0 4px 32px #0001;}
    h1 { text-align: center; margin-bottom: 10px;}
    .desc { color: #333; margin-bottom: 1em; text-align: center;}
    .tips { background: #f0f7ff; border-left: 5px solid #4390e1; padding: 1em 1.5em; border-radius: 8px; margin-bottom: 1.5em;}
    .url-row { display: flex; align-items: center; margin-bottom: 10px; gap: 8px; }
    .url-row input[type=text] { flex: 2; padding: 7px; border-radius: 5px; border: 1px solid #bbb; font-size: 1em;}
    .url-row select, .url-row input[type=file] { font-size: 1em;}
    .url-row label { margin-left: 10px; }
    .add-btn, .remove-btn { background: #eee; border: none; border-radius: 5px; padding: 7px 12px; cursor: pointer;}
    .add-btn { margin-left: 8px;}
    .remove-btn { color: #d00;}
    textarea { width: 100%; min-height: 160px; margin: 1em 0; border-radius: 8px; border: 1px solid #bbb; padding: 10px; font-size: 1em;}
    .result { background: #f5f5f5; padding: 1em; margin-top: 1.5em; border-radius: 8px; font-size: 1.1em;}
    .error { color: #b80000; font-weight: bold; margin-bottom: 1em; }
    button[type=submit] { background: #4390e1; color: #fff; border: none; padding: 12px 30px; font-size: 1em; border-radius: 8px; cursor: pointer; margin-top: 0.5em;}
    .csv-link { display: block; text-align: right; margin-top: 12px;}
    .tips small { font-size: 95%; color: #666;}
  </style>
</head>
<body>
<div class="container">
  <h1>Landing Page Analyzer</h1>
  <div class="desc">
    Analyze any landing page using Gemini AI and Playwright.<br>
    <strong>Choose "Manual" for protected sites (login, Cloudflare, bot-blockers) and upload your screenshot. Otherwise use "Auto".</strong>
  </div>
  <div class="tips">
    <b>Tips:</b><br>
    - <b>Taking a Full-Page Screenshot (Chrome):</b><br>
    <small>
      Method 1: Open the webpage, right-click, select "Inspect" (or press <b>Ctrl+Shift+I</b>), then press <b>Ctrl+Shift+P</b> (Cmd+Shift+P on Mac) to open the command palette. Type <b>screenshot</b> and choose "Capture full size screenshot".<br>
      Method 2: Click the three dots (Settings and more) in the top right of Chrome, select "More tools" &rarr; "Web capture". You can then select the area to capture or capture the full page.<br>
      <br>
      - <b>If the site requires login or blocks bots (e.g., Udemy, Brainstation, Cloudflare, Sucuri, or similar), choose "Manual" and upload your own screenshot.<br>
      - For "Manual", upload a PNG file. No naming needed &mdash; just attach it to the correct URL below.
      </b>
    </small>
  </div>
  <form method="POST" enctype="multipart/form-data" id="main-form">
    <div id="url-list">
      {% for idx, entry in enumerate(entries) %}
      <div class="url-row">
        <input type="text" name="urls" value="{{entry.url}}" placeholder="Paste landing page URL here..." required>
        <select name="modes">
          <option value="auto" {% if not entry.manual %}selected{% endif %}>Auto</option>
          <option value="manual" {% if entry.manual %}selected{% endif %}>Manual</option>
        </select>
        <input type="file" name="screenshots" accept="image/png" style="display: {% if entry.manual %}inline{% else %}none{% endif %};" />
        {% if not loop.first %}
        <button type="button" class="remove-btn" onclick="removeRow(this)">Remove</button>
        {% endif %}
      </div>
      {% endfor %}
    </div>
    <button type="button" class="add-btn" onclick="addRow()">Add another URL</button>
    <br>
    <label><b>Gemini Prompt (edit as needed):</b></label>
    <textarea name="prompt" id="prompt">{{prompt or default_prompt}}</textarea>
    <br>
    <button type="submit">Run Analysis</button>
  </form>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  {% if summary %}
    <div class="result">
      <h2>Analysis Summary:</h2>
      <pre style="font-size:1em;">{{summary}}</pre>
      <a class="csv-link" href="/download/csv">Download CSV</a>
    </div>
  {% endif %}
</div>
<script>
function addRow() {
  let urlList = document.getElementById('url-list');
  let newRow = document.createElement('div');
  newRow.className = "url-row";
  newRow.innerHTML = `
    <input type="text" name="urls" placeholder="Paste landing page URL here..." required>
    <select name="modes" onchange="toggleScreenshotInput(this)">
      <option value="auto" selected>Auto</option>
      <option value="manual">Manual</option>
    </select>
    <input type="file" name="screenshots" accept="image/png" style="display:none;" />
    <button type="button" class="remove-btn" onclick="removeRow(this)">Remove</button>
  `;
  urlList.appendChild(newRow);
}
function removeRow(btn) {
  btn.parentNode.parentNode.removeChild(btn.parentNode);
}
function toggleScreenshotInput(sel) {
  let input = sel.parentNode.querySelector('input[type="file"]');
  if (sel.value === "manual") {
    input.style.display = "inline";
    input.required = true;
  } else {
    input.style.display = "none";
    input.value = null;
    input.required = false;
  }
}
// Init event handlers on page load for edit/view
document.querySelectorAll('.url-row select').forEach(sel => {
  sel.addEventListener('change', function(){toggleScreenshotInput(sel)});
});
</script>
</body>
</html>
"""

def get_multimodal_analysis_from_gemini(page_content: str, image_bytes: bytes, provider_name: str, url: str, prompt_override=None) -> dict:
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

        prompt = (prompt_override or DEFAULT_PROMPT_TEMPLATE).format(
            provider_name=provider_name,
            url=url,
            prompt_text_section=prompt_text_section
        )

        flushprint("Sending prompt to Gemini")
        response = model.generate_content([prompt, image_for_api])
        # Try to extract JSON response only
        cleaned_json = response.text.strip()
        if "```json" in cleaned_json:
            cleaned_json = cleaned_json.split("```json")[1].split("```")[0].strip()
        flushprint("Gemini responded. Raw snippet:\n", cleaned_json[:1000])
        try:
            return json.loads(cleaned_json)
        except Exception as json_err:
            flushprint("JSON decode error:", json_err)
            flushprint("Gemini response was:\n", cleaned_json)
            raise Exception(f"Gemini response was not valid JSON: {json_err}\nResponse:\n{cleaned_json}")
    except Exception as e:
        flushprint("Gemini multimodal analysis failed:", e)
        raise

def analyze_landing_pages(request_form, request_files, prompt_override=None):
    flushprint("analyze_landing_pages called")
    urls = request_form.getlist("urls")
    modes = request_form.getlist("modes")
    screenshots = request_files.getlist("screenshots")
    all_course_data = []
    output_dir = "landing_page_analysis"
    os.makedirs(output_dir, exist_ok=True)
    flushprint(f"Output dir ready: {output_dir}")

    try:
        with sync_playwright() as p:
            flushprint("Playwright launched")
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36'
            )
            screenshot_idx = 0
            for idx, url in enumerate(urls):
                name = url.split("//")[-1].split("/")[1] if "/" in url.split("//")[-1] else url.split("//")[-1]
                provider_name = name.lower().replace(".", "_").replace("-", "_")
                mode = modes[idx] if idx < len(modes) else "auto"
                flushprint(f"Processing {provider_name} ({url}) mode={mode}")
                if mode == "manual":
                    if screenshot_idx >= len(screenshots):
                        all_course_data.append({"Platform": provider_name, "error": f"No screenshot uploaded for manual mode for {url}."})
                        continue
                    screenshot_file = screenshots[screenshot_idx]
                    screenshot_idx += 1
                    if not screenshot_file or not screenshot_file.filename:
                        all_course_data.append({"Platform": provider_name, "error": f"Missing screenshot file for {url}."})
                        continue
                    image_bytes = screenshot_file.read()
                    page_content = ""
                    try:
                        structured_data = get_multimodal_analysis_from_gemini(page_content, image_bytes, provider_name, url, prompt_override)
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
                        structured_data = get_multimodal_analysis_from_gemini(page_content, image_bytes, provider_name, url, prompt_override)
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

@app.route("/", methods=["GET", "POST"])
def index():
    flushprint("Index route called:", request.method)
    summary = None
    error = None
    csv_path = None

    if request.method == "POST":
        flushprint("POST data:", request.form)
        urls = request.form.getlist("urls")
        modes = request.form.getlist("modes")
        prompt = request.form.get("prompt")
        screenshots = request.files.getlist("screenshots")
        # Pre-fill form for UI
        entries = [{"url": u, "manual": (m == "manual")} for u, m in zip(urls, modes)]
        try:
            summary, csv_path = analyze_landing_pages(request.form, request.files, prompt)
            flushprint("Analysis summary done.")
        except Exception as e:
            error = str(e)
            flushprint("Error in POST analyze_landing_pages:", e)
    else:
        # Default: one blank row
        entries = [{"url": "", "manual": False}]
        prompt = DEFAULT_PROMPT_TEMPLATE

    return render_template_string(
        HTML,
        summary=summary,
        error=error,
        prompt=prompt,
        default_prompt=DEFAULT_PROMPT_TEMPLATE,
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

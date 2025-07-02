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

# ------------- LOG APP STARTUP -------------
flushprint("=== app.py is starting up ===")

try:
    import google.generativeai as genai
    from PIL import Image
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    flushprint("Imports successful")
except Exception as e:
    flushprint("Import error:", e)
    raise

# -- API KEY SETUP (use Render env vars for prod!) --
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

# ------------- NEW HTML TEMPLATE --------------------
HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Landing Page Analyzer (Gemini + Playwright)</title>
  <style>
    body {
      font-family: 'Inter', Arial, sans-serif;
      background: #f7f9fa;
      margin: 0;
      padding: 0;
    }
    .container {
      max-width: 700px;
      margin: 40px auto;
      background: #fff;
      border-radius: 14px;
      box-shadow: 0 8px 32px rgba(60,72,99,0.08);
      padding: 2.5em 2em 2em 2em;
    }
    h1 {
      text-align: center;
      color: #274690;
      margin-bottom: 0.5em;
      font-size: 2.2em;
    }
    .subtitle {
      color: #555;
      text-align: center;
      margin-bottom: 1.8em;
    }
    .tips {
      background: #e7f0fd;
      padding: 1em;
      border-radius: 10px;
      font-size: 1em;
      color: #274690;
      margin-bottom: 2em;
    }
    label {
      font-weight: 500;
      margin-top: 1em;
      display: block;
    }
    .urls-list {
      margin-bottom: 1.2em;
    }
    .url-row {
      background: #f5f7fa;
      border-radius: 8px;
      padding: 1em;
      display: flex;
      align-items: center;
      margin-bottom: 0.5em;
      gap: 1em;
    }
    .url-row input[type="text"] {
      flex: 2;
      margin-right: 8px;
      padding: 0.6em;
    }
    .url-row select {
      flex: 1;
      padding: 0.6em;
    }
    .url-row input[type="file"] {
      flex: 1.5;
      background: #fff;
      border: 1px solid #dde;
      padding: 3px 5px;
    }
    .url-row button[type="button"] {
      margin-left: 8px;
      background: #eee;
      border: none;
      border-radius: 6px;
      padding: 0.6em 1em;
      cursor: pointer;
      font-weight: 500;
    }
    .url-row button[type="button"]:hover {
      background: #e6e6e6;
    }
    textarea, input[type=text] {
      width: 100%;
      border-radius: 6px;
      border: 1px solid #dde;
      padding: 0.7em;
      font-size: 1em;
      margin-bottom: 1.2em;
    }
    button[type=submit] {
      display: block;
      margin: 1.6em auto 0 auto;
      background: #274690;
      color: #fff;
      font-size: 1.1em;
      border: none;
      border-radius: 8px;
      padding: 0.85em 2.5em;
      font-weight: 600;
      letter-spacing: 0.02em;
      box-shadow: 0 2px 10px rgba(60,72,99,0.12);
      cursor: pointer;
      transition: background 0.2s;
    }
    button[type=submit]:hover {
      background: #173259;
    }
    .result {
      background: #f7faff;
      padding: 1.2em;
      margin-top: 2em;
      border-radius: 12px;
      font-size: 1.05em;
    }
    .error {
      color: #b00020;
      font-weight: 500;
      margin-top: 1.2em;
      margin-bottom: 1em;
    }
    @media (max-width:600px) {
      .container { padding: 1.2em; }
      .url-row { flex-direction: column; gap: 0.5em; }
      .url-row input, .url-row select { width: 100%; }
    }
  </style>
  <script>
    function addUrlRow() {
      const list = document.getElementById('urls-list');
      const row = document.createElement('div');
      row.className = 'url-row';
      row.innerHTML = `
        <input type="text" name="url[]" placeholder="Paste landing page URL" required>
        <select name="mode[]"
          onchange="this.parentElement.querySelector('input[type=file]').style.display = (this.value==='manual') ? 'block' : 'none'">
          <option value="auto">Auto Analyze</option>
          <option value="manual">Manual Screenshot</option>
        </select>
        <input type="file" name="screenshot[]" accept="image/png,image/jpeg" style="display:none">
        <button type="button" onclick="this.parentElement.remove()">Remove</button>
      `;
      list.appendChild(row);
    }
    window.onload = function() {
      addUrlRow();
    }
  </script>
</head>
<body>
  <div class="container">
    <h1>Landing Page Analyzer</h1>
    <div class="subtitle">Analyze above-the-fold value prop, CTAs, and trust for any web page. Uses Google Gemini + Playwright.</div>
    <div class="tips">
      <strong>Tips:</strong><br>
      1. <b>Most sites:</b> Use "Auto Analyze" – just paste the URL!<br>
      2. <b>Login-required sites (Udemy, Brainstation, etc):</b> <br>
         - Take a full-page screenshot (<a href="https://support.microsoft.com/en-us/windows/windows-11-screenshots-6d94867b-dc3a-5395-cb6c-5b766c41b8c2" target="_blank">Windows</a>; <a href="https://support.apple.com/en-us/HT201361" target="_blank">Mac</a>).<br>
         - Name file as <code>platform_manual.png</code> (e.g. <code>udemy_manual.png</code>).<br>
         - Select "Manual Screenshot" and upload it.<br>
      3. <b>Supported images:</b> PNG or JPEG, ideally full-page.<br>
      4. <b>Repeat:</b> Add as many URLs as you want.
    </div>
    <form method="POST" enctype="multipart/form-data">
      <label>Landing Pages:</label>
      <div id="urls-list" class="urls-list"></div>
      <button type="button" onclick="addUrlRow()" style="margin: 0 0 1em 0; background: #60a5fa; color: #fff; border-radius: 8px; padding: 0.5em 1.2em;">Add Another URL</button>
      <label>Prompt for Gemini (optional):</label>
      <textarea name="prompt" rows="4" placeholder="e.g., Describe the value prop, CTA, and trust elements above the fold...">{{prompt or default_prompt}}</textarea>
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

# -- Gemini Analysis Function --
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
        prompt = prompt_override or "Describe the value prop, CTA, and trust elements above the fold..."
        flushprint("Sending prompt to Gemini")
        response = model.generate_content([prompt, image_for_api])
        cleaned = response.text.strip()
        flushprint("Gemini responded. Raw snippet:")
        flushprint(cleaned[:400])
        # Try to parse JSON if provided, else just return as plain text.
        try:
            return json.loads(cleaned)
        except Exception:
            # Not JSON: return as {"Platform": ..., "response": ...}
            return {
                "Platform": provider_name,
                "Response": cleaned
            }
    except Exception as e:
        flushprint("Gemini multimodal analysis failed:", e)
        raise

# -- Main analyzer function --
def analyze_landing_pages(landing_pages, prompt_override=None):
    flushprint("analyze_landing_pages called")
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
            for lp in landing_pages:
                flushprint(f"Processing {lp['name']} ({lp['url']}) manual={lp.get('manual')}")
                if lp.get("manual", False):
                    manual_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{lp['name']}_manual.png")
                    if not os.path.exists(manual_path):
                        flushprint(f"Manual screenshot not found: {manual_path}")
                        all_course_data.append({"Platform": lp['name'], "error": f"Manual screenshot '{manual_path}' not found."})
                        continue
                    with open(manual_path, "rb") as f:
                        image_bytes = f.read()
                    page_content = ""
                    try:
                        structured_data = get_multimodal_analysis_from_gemini(page_content, image_bytes, lp['name'], lp['url'], prompt_override)
                        all_course_data.append(structured_data)
                        flushprint(f"Gemini result added for {lp['name']}")
                    except Exception as e:
                        flushprint(f"Error for manual {lp['name']}:", e)
                        all_course_data.append({"Platform": lp['name'], "error": str(e)})
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
                        structured_data = get_multimodal_analysis_from_gemini(page_content, image_bytes, lp['name'], lp['url'], prompt_override)
                        all_course_data.append(structured_data)
                        flushprint(f"Playwright + Gemini result added for {lp['name']}")
                    except Exception as e:
                        flushprint(f"Error for auto {lp['name']}:", e)
                        all_course_data.append({"Platform": lp['name'], "error": str(e)})
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

# --- Main routes ---
@app.route("/", methods=["GET", "POST"])
def index():
    flushprint("Index route called:", request.method)
    summary = None
    error = None
    csv_path = None
    prompt = ''
    default_prompt = f"""
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

    if request.method == "POST":
        urls = request.form.getlist("url[]")
        modes = request.form.getlist("mode[]")
        screenshots = request.files.getlist("screenshot[]")
        flushprint(f"POST urls: {urls}")
        flushprint(f"POST modes: {modes}")
        landing_pages = []
        for idx, url in enumerate(urls):
            mode = modes[idx]
            name = url.split("//")[-1].split("/")[1] if "/" in url.split("//")[-1] else url.split("//")[-1]
            name = name.lower().replace(".", "_").replace("-", "_")
            if mode == "manual" and screenshots[idx] and screenshots[idx].filename:
                manual_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{name}_manual.png")
                screenshots[idx].save(manual_path)
                landing_pages.append({"name": name, "url": url, "manual": True})
            else:
                landing_pages.append({"name": name, "url": url, "manual": False})
        prompt = request.form.get("prompt") or default_prompt
        flushprint("Landing pages dict:", landing_pages)
        try:
            summary, csv_path = analyze_landing_pages(landing_pages, prompt)
            flushprint("Analysis summary done.")
        except Exception as e:
            error = str(e)
            flushprint("Error in POST analyze_landing_pages:", e)

    return render_template_string(HTML, summary=summary, error=error, prompt=prompt, default_prompt=default_prompt)

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

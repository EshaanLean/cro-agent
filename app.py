import os
import io
import json
import pandas as pd
from flask import Flask, request, render_template_string, send_file
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import google.generativeai as genai
from PIL import Image

# -- API KEY SETUP (use Render env vars for prod!) --
API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    raise ValueError("GEMINI_API_KEY env var not set! Add it on Render.com dashboard.")
genai.configure(api_key=API_KEY)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = "manual_screenshots"
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ------------- HTML TEMPLATE --------------------
HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Landing Page Analyzer (Gemini + Playwright)</title>
  <style>
    body { font-family: Arial; margin: 2em; }
    .container { max-width: 720px; margin: auto; }
    textarea, input[type=text] { width: 100%; }
    .file-upload { margin-bottom: 1em; }
    label { font-weight: bold; }
    .result { background: #f5f5f5; padding: 1em; margin-top: 1em; border-radius: 8px; }
    .error { color: red; }
  </style>
</head>
<body>
<div class="container">
  <h1>Landing Page Analyzer (Gemini + Playwright)</h1>
  <form method="POST" enctype="multipart/form-data">
    <label>Landing Page URLs (one per line):</label>
    <textarea name="urls" rows="6" required>{{urls or ""}}</textarea><br><br>

    <label>Prompt for Gemini:</label>
    <textarea name="prompt" rows="4" required>{{prompt or default_prompt}}</textarea><br><br>

    <label>Manual Screenshot Uploads:</label>
    <div class="file-upload">
      <input type="file" name="screenshots" multiple>
      <small>For protected sites (Brainstation, Udemy, etc) upload PNGs. Name file as &lt;name&gt;_manual.png (e.g. udemy_manual.png).</small>
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

# -- Utility: save uploads to manual_screenshots folder --
def save_manual_screenshots(files):
    uploaded_names = []
    for file in files.getlist("screenshots"):
        if file.filename:
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
            file.save(save_path)
            uploaded_names.append(file.filename)
    return uploaded_names

# -- Gemini Analysis Function (from your code) --
def get_multimodal_analysis_from_gemini(page_content: str, image_bytes: bytes, provider_name: str, url: str, prompt_override=None) -> dict:
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

    prompt = prompt_override or f"""
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
      "Purchase or Lead Gen Form": "...",
      "Primary CTA": "...",
      "Above the Fold - Headline": "...",
      "Above the Fold - Trust Elements": "...",
      "Above the Fold - Other Elements": "...",
      "Above the Fold - Creative (Yes/No)": "...",
      "Above the Fold - Creative Type": "...",
      "Above the Fold - Creative Position": "...",
      "Above the Fold - # of CTAs": "...",
      "Above the Fold - CTA / Form Position": "...",
      "Primary CTA Just for Free Trial": "...",
      "Secondary CTA": "...",
      "Clickable Logo": "...",
      "Navigation Bar": "..."
    }}

    Return ONLY the valid JSON object, with no other text, comments, or markdown formatting.
    """

    response = model.generate_content([prompt, image_for_api])
    cleaned_json = response.text.strip().replace("```json", "").replace("```", "")
    return json.loads(cleaned_json)

# -- Main analyzer function (mixes manual+auto) --
def analyze_landing_pages(landing_pages, prompt_override=None):
    all_course_data = []
    output_dir = "landing_page_analysis"
    os.makedirs(output_dir, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
        )

        for lp in landing_pages:
            if lp.get("manual", False):
                # Use uploaded screenshot
                manual_file = f"{lp['name']}_manual.png"
                manual_path = os.path.join(app.config['UPLOAD_FOLDER'], manual_file)
                if not os.path.exists(manual_path):
                    all_course_data.append({"Platform": lp['name'], "error": f"Manual screenshot '{manual_file}' not found."})
                    continue
                with open(manual_path, "rb") as f:
                    image_bytes = f.read()
                page_content = ""
                structured_data = get_multimodal_analysis_from_gemini(page_content, image_bytes, lp['name'], lp['url'], prompt_override)
                all_course_data.append(structured_data)
            else:
                # Automated Playwright logic
                page = context.new_page()
                try:
                    page.goto(lp["url"], wait_until="domcontentloaded", timeout=120000)
                    page.wait_for_timeout(10000)
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

    df = pd.DataFrame(all_course_data)
    df.to_csv(os.path.join(output_dir, "competitive_analysis_data.csv"), index=False)
    # Generate a simple summary for demo
    summary = df.to_string(index=False)
    return summary, os.path.join(output_dir, "competitive_analysis_data.csv")

# --- Main routes ---
@app.route("/", methods=["GET", "POST"])
def index():
    summary = None
    error = None
    csv_path = None
    urls = ''
    prompt = ''
    default_prompt = "Describe the value prop, CTA, and trust elements above the fold..."

    if request.method == "POST":
        urls = request.form.get("urls")
        prompt = request.form.get("prompt")
        uploaded_files = request.files

        # Save manual screenshots if uploaded
        save_manual_screenshots(uploaded_files)

        # Parse URLs (add your own logic for manual/auto)
        url_list = [u.strip() for u in urls.splitlines() if u.strip()]
        landing_pages = []
        for url in url_list:
            # If user uploaded manual screenshot, set manual=True if matching file exists
            base_name = url.split("//")[-1].split("/")[1] if "/" in url.split("//")[-1] else url.split("//")[-1]
            name = base_name.lower().replace(".", "_").replace("-", "_")
            manual_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{name}_manual.png")
            landing_pages.append({
                "name": name,
                "url": url,
                "manual": os.path.exists(manual_path)
            })
        try:
            summary, csv_path = analyze_landing_pages(landing_pages, prompt)
        except Exception as e:
            error = str(e)

    return render_template_string(HTML, summary=summary, error=error, urls=urls, prompt=prompt, default_prompt=default_prompt)

@app.route('/download/csv')
def download_csv():
    # Serves the last analysis CSV for download (you can improve with session-based paths)
    path = "landing_page_analysis/competitive_analysis_data.csv"
    if not os.path.exists(path):
        return "No file available", 404
    return send_file(path, as_attachment=True, download_name="competitive_analysis_data.csv")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))  # Use $PORT if set (Render), else default to 10000 for local dev
    # host="0.0.0.0" makes your app accessible externally
    app.run(host="0.0.0.0", port=port)

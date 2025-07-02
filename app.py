import os
import io
import json
import sys
import pandas as pd
import re
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

# ------------- HTML TEMPLATE (no changes) --------------------
HTML = HTML = """
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
    flushprint("save_manual_screenshots called")
    for file in files.getlist("screenshots"):
        flushprint("Got file:", file.filename)
        if file.filename:
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
            file.save(save_path)
            flushprint("Saved manual screenshot:", save_path)
            uploaded_names.append(file.filename)
    return uploaded_names

# -- Gemini Analysis Function (from your code) --
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

        prompt = prompt_override or f"""
        (your same prompt...)
        """

        flushprint("Sending prompt to Gemini")
        response = model.generate_content([prompt, image_for_api])
        raw_response = getattr(response, 'text', None)
        if not raw_response:
            flushprint("Gemini returned empty response or missing .text attribute!")
            raise ValueError("Gemini returned empty response or missing .text attribute!")

        cleaned_json = raw_response.strip().replace("```json", "").replace("```", "")
        flushprint("Gemini responded. Raw snippet:\n", cleaned_json[:400])

        # Optionally, dump the full response to a file for debugging
        try:
            with open("last_gemini_response.txt", "w", encoding="utf-8") as f:
                f.write(cleaned_json)
        except Exception as dump_err:
            flushprint("Could not write Gemini response to file:", dump_err)

        # Try extracting the first {...} block (for safety)
        matches = re.findall(r'\{.*\}', cleaned_json, re.DOTALL)
        if matches:
            cleaned_json = matches[0]

        # Try parsing JSON, print details if it fails
        try:
            parsed = json.loads(cleaned_json)
            flushprint("JSON parsed OK")
            return parsed
        except json.JSONDecodeError as je:
            flushprint("JSON decode error:", je)
            flushprint("Gemini response was:\n", cleaned_json)
            raise ValueError(f"Gemini response was not valid JSON: {je}\nResponse:\n{cleaned_json}")

    except Exception as e:
        flushprint("Gemini multimodal analysis failed:", e)
        raise


# -- Main analyzer function (mixes manual+auto) --
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
                    manual_file = f"{lp['name']}_manual.png"
                    manual_path = os.path.join(app.config['UPLOAD_FOLDER'], manual_file)
                    if not os.path.exists(manual_path):
                        flushprint(f"Manual screenshot not found: {manual_file}")
                        all_course_data.append({"Platform": lp['name'], "error": f"Manual screenshot '{manual_file}' not found."})
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
    urls = ''
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
        flushprint("POST data:", request.form)
        urls = request.form.get("urls")
        prompt = request.form.get("prompt")
        uploaded_files = request.files

        save_manual_screenshots(uploaded_files)
        flushprint("Manual screenshots saved (if any)")

        url_list = [u.strip() for u in (urls or "").splitlines() if u.strip()]
        flushprint("Parsed URLs:", url_list)
        landing_pages = []
        for url in url_list:
            base_name = url.split("//")[-1].split("/")[1] if "/" in url.split("//")[-1] else url.split("//")[-1]
            name = base_name.lower().replace(".", "_").replace("-", "_")
            manual_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{name}_manual.png")
            landing_pages.append({
                "name": name,
                "url": url,
                "manual": os.path.exists(manual_path)
            })
        flushprint("Landing pages dict:", landing_pages)
        try:
            summary, csv_path = analyze_landing_pages(landing_pages, prompt)
            flushprint("Analysis summary done.")
        except Exception as e:
            error = str(e)
            flushprint("Error in POST analyze_landing_pages:", e)

    return render_template_string(HTML, summary=summary, error=error, urls=urls, prompt=prompt, default_prompt=default_prompt)

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

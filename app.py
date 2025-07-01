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

# ------------- HTML TEMPLATE (no changes) --------------------
HTML = """...""" # (keep your original HTML)

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
        cleaned_json = response.text.strip().replace("```json", "").replace("```", "")
        flushprint("Gemini responded. JSON parsed.")
        return json.loads(cleaned_json)
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
    default_prompt = "Describe the value prop, CTA, and trust elements above the fold..."

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

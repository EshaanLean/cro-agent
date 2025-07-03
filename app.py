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
    flushprint("save_manual_screenshots called")
    for file in files.getlist("screenshots"):
        flushprint("Got file:", file.filename)
        if file.filename:
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
            file.save(save_path)
            flushprint("Saved manual screenshot:", save_path)
            uploaded_names.append(file.filename)
    return uploaded_names

# -- Gemini Analysis Function (FIXED) --
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

        # FIXED: Complete prompt instead of placeholder
        prompt = prompt_override or f"""
        Analyze this landing page screenshot and provide a structured analysis in JSON format.
        Focus on the above-the-fold content and provide the following information:

        {prompt_text_section}

        Please analyze the landing page and return a JSON object with the following structure:
        {{
            "Platform": "{provider_name}",
            "URL": "{url}",
            "Value_Proposition": "Main value proposition or headline",
            "CTA_Primary": "Primary call-to-action text and placement",
            "Trust_Elements": "Trust signals, testimonials, social proof",
            "Visual_Design": "Description of visual design and layout",
            "Target_Audience": "Apparent target audience",
            "Unique_Selling_Points": "Key differentiators mentioned",
            "Pricing_Mentioned": "Any pricing information visible",
            "Course_Type": "Type of course or program offered",
            "Key_Features": "Main features or benefits highlighted"
        }}

        Return only valid JSON, no additional text or markdown formatting.
        """

        flushprint("Sending prompt to Gemini")
        response = model.generate_content([prompt, image_for_api])
        
        if not response.text:
            raise Exception("Empty response from Gemini API")
            
        cleaned_json = response.text.strip().replace("```json", "").replace("```", "")
        flushprint("Gemini responded. Attempting JSON parse...")
        
        # FIXED: Better error handling and ensure Platform is set
        try:
            result = json.loads(cleaned_json)
            # Ensure Platform is always set correctly
            result["Platform"] = provider_name
            result["URL"] = url
            flushprint("JSON parsed successfully")
            return result
        except json.JSONDecodeError as e:
            flushprint(f"JSON parse error: {e}")
            flushprint(f"Raw response: {cleaned_json[:500]}")
            # Return structured error data instead of raising
            return {
                "Platform": provider_name,
                "URL": url,
                "error": f"JSON parse error: {str(e)}",
                "raw_response": cleaned_json[:500]
            }
            
    except Exception as e:
        flushprint("Gemini multimodal analysis failed:", e)
        # Return structured error data instead of raising
        return {
            "Platform": provider_name,
            "URL": url,
            "error": f"Gemini API error: {str(e)}"
        }

# -- Main analyzer function (FIXED) --
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
                    # MANUAL SCREENSHOT PROCESSING
                    manual_file = f"{lp['name']}_manual.png"
                    manual_path = os.path.join(app.config['UPLOAD_FOLDER'], manual_file)
                    if not os.path.exists(manual_path):
                        flushprint(f"Manual screenshot not found: {manual_file}")
                        all_course_data.append({
                            "Platform": lp['name'], 
                            "URL": lp['url'],
                            "error": f"Manual screenshot '{manual_file}' not found."
                        })
                        continue
                    
                    try:
                        with open(manual_path, "rb") as f:
                            image_bytes = f.read()
                        page_content = ""
                        structured_data = get_multimodal_analysis_from_gemini(
                            page_content, image_bytes, lp['name'], lp['url'], prompt_override
                        )
                        all_course_data.append(structured_data)
                        flushprint(f"Manual analysis completed for {lp['name']}")
                    except Exception as e:
                        flushprint(f"Error processing manual screenshot for {lp['name']}: {e}")
                        all_course_data.append({
                            "Platform": lp['name'], 
                            "URL": lp['url'],
                            "error": f"Manual processing error: {str(e)}"
                        })
                else:
                    # AUTOMATIC SCREENSHOT PROCESSING
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
                        structured_data = get_multimodal_analysis_from_gemini(
                            page_content, image_bytes, lp['name'], lp['url'], prompt_override
                        )
                        all_course_data.append(structured_data)
                        flushprint(f"Automatic analysis completed for {lp['name']}")
                    except PlaywrightTimeoutError:
                        flushprint(f"Timeout error for {lp['name']}")
                        all_course_data.append({
                            "Platform": lp['name'], 
                            "URL": lp['url'],
                            "error": "Page load timeout"
                        })
                    except Exception as e:
                        flushprint(f"Error for auto processing {lp['name']}: {e}")
                        all_course_data.append({
                            "Platform": lp['name'], 
                            "URL": lp['url'],
                            "error": f"Auto processing error: {str(e)}"
                        })
                    finally:
                        page.close()
                        
            browser.close()
            flushprint("Browser closed")
            
    except Exception as e:
        flushprint("Fatal error in analyze_landing_pages:", e)
        # Don't raise - return what we have
        all_course_data.append({
            "Platform": "SYSTEM_ERROR",
            "URL": "N/A", 
            "error": f"Fatal error: {str(e)}"
        })

    # FIXED: Ensure we always have data to save
    if not all_course_data:
        all_course_data.append({
            "Platform": "NO_DATA",
            "URL": "N/A",
            "error": "No data was collected"
        })

    # Save CSV
    df = pd.DataFrame(all_course_data)
    csv_path = os.path.join(output_dir, "competitive_analysis_data.csv")
    df.to_csv(csv_path, index=False)
    flushprint(f"CSV saved with {len(all_course_data)} records")
    
    # Create summary
    summary = f"Analysis completed for {len(all_course_data)} landing pages:\n\n"
    summary += df.to_string(index=False)
    
    return summary, csv_path

# --- Main routes ---
@app.route("/", methods=["GET", "POST"])
def index():
    flushprint("Index route called:", request.method)
    summary = None
    error = None
    csv_path = None
    urls = ''
    prompt = ''
    default_prompt = "Analyze this landing page and provide detailed insights about value proposition, call-to-action, trust elements, and overall design strategy."

    if request.method == "POST":
        flushprint("POST data:", request.form)
        urls = request.form.get("urls")
        prompt = request.form.get("prompt")
        uploaded_files = request.files

        # Save manual screenshots
        uploaded_names = save_manual_screenshots(uploaded_files)
        flushprint(f"Manual screenshots saved: {uploaded_names}")

        # Parse URLs
        url_list = [u.strip() for u in (urls or "").splitlines() if u.strip()]
        flushprint("Parsed URLs:", url_list)
        
        if not url_list:
            error = "Please provide at least one URL"
        else:
            landing_pages = []
            for url in url_list:
                # Extract name from URL
                try:
                    domain_part = url.split("//")[-1].split("/")[0]
                    if "." in domain_part:
                        base_name = domain_part.split(".")[0]
                    else:
                        base_name = domain_part
                    name = base_name.lower().replace("-", "_").replace(".", "_")
                    
                    # Check if manual screenshot exists
                    manual_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{name}_manual.png")
                    is_manual = os.path.exists(manual_path)
                    
                    landing_pages.append({
                        "name": name,
                        "url": url,
                        "manual": is_manual
                    })
                except Exception as e:
                    flushprint(f"Error parsing URL {url}: {e}")
                    landing_pages.append({
                        "name": "unknown",
                        "url": url,
                        "manual": False
                    })
            
            flushprint("Landing pages prepared:", landing_pages)
            
            try:
                summary, csv_path = analyze_landing_pages(landing_pages, prompt)
                flushprint("Analysis completed successfully")
            except Exception as e:
                error = f"Analysis failed: {str(e)}"
                flushprint("Error in analysis:", e)

    return render_template_string(HTML, summary=summary, error=error, urls=urls, prompt=prompt, default_prompt=default_prompt)

@app.route('/download/csv')
def download_csv():
    flushprint("Download CSV requested")
    path = "landing_page_analysis/competitive_analysis_data.csv"
    if not os.path.exists(path):
        flushprint("CSV not found")
        return "No CSV file available. Please run an analysis first.", 404
    flushprint("CSV found, sending")
    return send_file(path, as_attachment=True, download_name="competitive_analysis_data.csv")

@app.route("/ping")
def ping():
    flushprint("pinged /ping endpoint")
    return "pong"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flushprint(f"Starting Flask app on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)
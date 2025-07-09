import os
import io
import sys
import re
import json
import urllib.parse
from datetime import datetime
import pandas as pd
from flask import Flask, request, render_template_string, send_file, jsonify
import zipfile
from PIL import Image, ImageOps
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor

def get_db_conn():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise Exception("DATABASE_URL not set!")

    # For Render PostgreSQL, ensure SSL mode is set correctly using URL parsing
    if "render.com" in db_url or "dpg-" in db_url:
        # Parse the URL into its components
        parts = urllib.parse.urlparse(db_url)
        # Parse the query string into a dictionary
        query_params = urllib.parse.parse_qs(parts.query)

        # Remove the incorrect 'ssl' parameter if it exists from a previous config
        query_params.pop('ssl', None)

        # Set the correct 'sslmode' parameter
        query_params['sslmode'] = ['require'] # parse_qs expects values in a list

        # Rebuild the query string and then the full URL
        new_query = urllib.parse.urlencode(query_params, doseq=True)
        parts = parts._replace(query=new_query)
        db_url = urllib.parse.urlunparse(parts)

    return psycopg2.connect(db_url, cursor_factory=RealDictCursor)

def init_database():
    """Initialize database tables if they don't exist"""
    try:
        conn = get_db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Create screenshots table
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS screenshots (
                            id SERIAL PRIMARY KEY,
                            name VARCHAR(255) NOT NULL,
                            url TEXT,
                            image BYTEA,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    
                    # Create analysis_results table for CSV and reports
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS analysis_results (
                            id SERIAL PRIMARY KEY,
                            session_id VARCHAR(255) NOT NULL,
                            file_type VARCHAR(50) NOT NULL,
                            file_name VARCHAR(255) NOT NULL,
                            content TEXT,
                            file_data BYTEA,
                            client_name VARCHAR(255),
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    
                    print("Database tables initialized successfully")
        finally:
            conn.close()
    except Exception as e:
        print(f"Error initializing database: {e}")
        print("App will continue running, but database features may not work")
        # Don't raise the exception - let the app start anyway

def flushprint(*args, **kwargs):
    print(*args, **kwargs)
    sys.stdout.flush()

flushprint("=== Dynamic Landing Page Analyzer starting up ===")

# Initialize database on startup
init_database()

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    flushprint("ERROR: GEMINI_API_KEY env var not set!")
    raise ValueError("GEMINI_API_KEY env var not set! Add it on your hosting platform.")
else:
    flushprint("GEMINI_API_KEY loaded")

genai.configure(api_key=API_KEY)
flushprint("Gemini configured")

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = "manual_screenshots"
app.config['OUTPUT_FOLDER'] = "analysis_results"
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

MAX_DIM = 2048

def _prepare_image(pil_img: Image.Image) -> Image.Image:
    if max(pil_img.size) > MAX_DIM:
        pil_img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)
    if pil_img.mode in ("RGBA", "P"):
        pil_img = ImageOps.exif_transpose(pil_img.convert("RGB"))
    return pil_img

def _extract_json(text: str) -> dict:
    flushprint(f"Attempting to extract JSON from response (length: {len(text)})")
    json_match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if json_match:
        flushprint("Found JSON in code block")
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError as e:
            flushprint(f"JSON decode error in code block: {e}")

    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        json_str = text[start:end+1]
        flushprint("Found JSON brackets, attempting parse")
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            flushprint(f"JSON decode error: {e}")

    json_objects = re.findall(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text)
    for json_candidate in json_objects:
        try:
            result = json.loads(json_candidate)
            flushprint("Found valid JSON object")
            return result
        except json.JSONDecodeError:
            continue

    flushprint("Attempting to construct JSON from key-value pairs")
    try:
        lines = text.split('\n')
        json_data = {}
        for line in lines:
            if ':' in line and not line.strip().startswith('#'):
                key_match = re.search(r'"([^"]+)":\s*"([^"]*)"', line)
                if key_match:
                    json_data[key_match.group(1)] = key_match.group(2)
        if json_data:
            flushprint(f"Constructed JSON from {len(json_data)} key-value pairs")
            return json_data
    except Exception as e:
        flushprint(f"Error constructing JSON: {e}")

    snippet = text.strip().replace('\n', ' ')[:500]
    flushprint(f"All JSON extraction methods failed. Response snippet: {snippet}")
    raise ValueError(f"No valid JSON found in model response. Response snippet: {snippet}")

def save_screenshot_to_db(name, url, image_bytes):
    """Save screenshot to database"""
    try:
        conn = get_db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO screenshots (name, url, image) VALUES (%s, %s, %s)",
                        (name, url, psycopg2.Binary(image_bytes))
                    )
            print(f"Saved screenshot for {name} ({url}) in DB, bytes: {len(image_bytes)}")
        finally:
            conn.close()
    except Exception as e:
        print(f"Error saving screenshot to database: {e}")
        # Don't raise - continue without DB save

def save_analysis_result_to_db(session_id, file_type, file_name, content=None, file_data=None, client_name=None):
    """Save analysis results (CSV, reports) to database"""
    try:
        conn = get_db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO analysis_results 
                           (session_id, file_type, file_name, content, file_data, client_name) 
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (session_id, file_type, file_name, content, 
                         psycopg2.Binary(file_data) if file_data else None, client_name)
                    )
            print(f"Saved {file_type} to database: {file_name}")
        finally:
            conn.close()
    except Exception as e:
        print(f"Error saving {file_type} to database: {e}")
        # Don't raise - continue without DB save

def get_screenshot_from_db(name, url=None):
    try:
        conn = get_db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    if url:
                        cur.execute(
                            "SELECT image FROM screenshots WHERE name=%s AND url=%s ORDER BY created_at DESC LIMIT 1",
                            (name, url)
                        )
                    else:
                        cur.execute(
                            "SELECT image FROM screenshots WHERE name=%s ORDER BY created_at DESC LIMIT 1",
                            (name,)
                        )
                    row = cur.fetchone()
                    if row:
                        return row["image"]
                    else:
                        return None
        finally:
            conn.close()
    except Exception as e:
        print(f"Error retrieving screenshot from database: {e}")
        return None

def get_analysis_result_from_db(session_id, file_type):
    """Retrieve analysis results from database"""
    try:
        conn = get_db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT file_name, content, file_data FROM analysis_results WHERE session_id=%s AND file_type=%s ORDER BY created_at DESC LIMIT 1",
                        (session_id, file_type)
                    )
                    row = cur.fetchone()
                    if row:
                        return row
                    else:
                        return None
        finally:
            conn.close()
    except Exception as e:
        print(f"Error retrieving analysis result from database: {e}")
        return None

@app.route('/screenshot/<name>')
def serve_screenshot(name):
    image_bytes = get_screenshot_from_db(name, None)
    if image_bytes:
        return send_file(io.BytesIO(image_bytes), mimetype='image/png')
    else:
        return "Not found", 404
    
@app.route('/screenshots')
def list_screenshots():
    try:
        conn = get_db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id, name, url, created_at FROM screenshots ORDER BY created_at DESC LIMIT 20;")
                    rows = cur.fetchall()
            return jsonify(rows)
        finally:
            conn.close()
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500

@app.route('/analysis_results')
def list_analysis_results():
    """List recent analysis results"""
    try:
        conn = get_db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT session_id, file_type, file_name, client_name, created_at 
                        FROM analysis_results 
                        ORDER BY created_at DESC LIMIT 50
                    """)
                    rows = cur.fetchall()
            return jsonify(rows)
        finally:
            conn.close()
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500

# ------------- HTML TEMPLATE --------------------
HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>Dynamic Landing Page Analyzer</title>
  <style>
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; background-color: #f5f7fa; }
    .container { max-width: 1200px; margin: auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
    h1 { color: #2c3e50; text-align: center; margin-bottom: 30px; }
    .section { margin-bottom: 30px; padding: 20px; background: #f8f9fa; border-radius: 8px; border-left: 4px solid #007bff; }
    .section h3 { color: #495057; margin-top: 0; }
    textarea, input[type=text], select { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 5px; font-size: 14px; }
    .url-input { margin-bottom: 15px; }
    .url-row { display: flex; gap: 10px; margin-bottom: 10px; align-items: center; }
    .url-row input[type=text] { flex: 2; }
    .url-row select { flex: 1; }
    .url-row button { padding: 10px 15px; background: #dc3545; color: white; border: none; border-radius: 5px; cursor: pointer; }
    .add-url-btn { background: #28a745; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; margin-bottom: 15px; }
    .analyze-btn { background: #007bff; color: white; border: none; padding: 15px 30px; border-radius: 5px; cursor: pointer; font-size: 16px; width: 100%; margin-top: 20px; }
    .analyze-btn:hover { background: #0056b3; }
    .file-upload { margin-top: 15px; }
    .result { background: #f8f9fa; padding: 20px; margin-top: 20px; border-radius: 8px; border: 1px solid #dee2e6; }
    .error { color: #dc3545; background: #f8d7da; border: 1px solid #f5c6cb; padding: 10px; border-radius: 5px; }
    .success { color: #155724; background: #d4edda; border: 1px solid #c3e6cb; padding: 10px; border-radius: 5px; }
    .screenshot-tip { background: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 5px; margin-top: 10px; }
    .screenshot-tip h4 { color: #856404; margin-top: 0; }
    .screenshot-tip code { background: #f8f9fa; padding: 2px 5px; border-radius: 3px; }
    .download-section { margin-top: 20px; padding: 20px; background: #e9ecef; border-radius: 8px; }
    .download-btn { background: #6c757d; color: white; text-decoration: none; padding: 10px 20px; border-radius: 5px; margin-right: 10px; display: inline-block; }
    .loading { text-align: center; margin: 20px 0; }
    .spinner { border: 4px solid #f3f3f3; border-top: 4px solid #007bff; border-radius: 50%; width: 40px; height: 40px; animation: spin 1s linear infinite; margin: 0 auto; }
    @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
    .debug-section { margin-top: 20px; padding: 15px; background: #e3f2fd; border-radius: 5px; }
    .debug-btn { background: #2196f3; color: white; text-decoration: none; padding: 8px 15px; border-radius: 3px; margin-right: 10px; display: inline-block; font-size: 12px; }
  </style>
</head>
<body>
<div class="container">
  <h1>🚀 Dynamic Landing Page Analyzer</h1>
  
  <!-- Debug Section -->
  <div class="debug-section">
    <h4>🔍 Debug & Database Status</h4>
    <a href="/test-db" class="debug-btn" target="_blank">Test DB Connection</a>
    <a href="/check-env" class="debug-btn" target="_blank">Check Environment</a>
    <a href="/screenshots" class="debug-btn" target="_blank">View Screenshots DB</a>
    <a href="/analysis_results" class="debug-btn" target="_blank">View Analysis Results DB</a>
    <p><small>Check these links to diagnose connection issues and see stored data</small></p>
  </div>

  <form method="POST" enctype="multipart/form-data" id="analysisForm">
    <div class="section">
      <h3>1. Configure URLs</h3>
      <div id="urlInputs">
        <div class="url-row">
          <input type="text" name="urls[]" placeholder="Enter landing page URL" required>
          <select name="types[]" required>
            <option value="">Select Type</option>
            <option value="client">Client (Your Site)</option>
            <option value="competitor">Competitor</option>
            <option value="manual">Manual Screenshot</option>
          </select>
          <button type="button" onclick="removeUrl(this)">Remove</button>
        </div>
      </div>
      <button type="button" class="add-url-btn" onclick="addUrl()">+ Add Another URL</button>
    </div>
    <div class="section">
      <h3>2. Analysis Prompt</h3>
      <textarea name="prompt" rows="6" placeholder="Enter your analysis prompt...">{{ prompt or default_prompt }}</textarea>
    </div>
    <div class="section">
      <h3>3. Manual Screenshots (Optional)</h3>
      <input type="file" name="screenshots" multiple accept=".png,.jpg,.jpeg">
      <div class="screenshot-tip">
        <h4>📸 How to Take Manual Screenshots:</h4>
        <p><strong>For protected/complex sites:</strong></p>
        <ol>
          <li>Open the website in Chrome</li>
          <li>Press <code>F12</code> to open Developer Tools</li>
          <li>Press <code>Ctrl+Shift+P</code> (or <code>Cmd+Shift+P</code> on Mac)</li>
          <li>Type "screenshot" and select <strong>"Capture full size screenshot"</strong></li>
          <li>Save the image with format: <code>[sitename]_manual.png</code></li>
        </ol>
        <p><strong>Example:</strong> For udemy.com, save as <code>udemy_manual.png</code></p>
      </div>
    </div>
    <button type="submit" class="analyze-btn" onclick="showLoading()">🔍 Run Analysis</button>
  </form>
  <div id="loading" class="loading" style="display:none;">
    <div class="spinner"></div>
    <p>Analyzing landing pages... This may take a few minutes.</p>
  </div>
  {% if error %}
    <div class="error">{{ error }}</div>
  {% endif %}
  {% if success %}
    <div class="success">{{ success }}</div>
  {% endif %}
  {% if summary %}
    <div class="result">
      <h2>📊 Analysis Complete!</h2>
      <div class="download-section">
        <h3>Download Results:</h3>
        <a href="/download/csv" class="download-btn">📄 Download CSV Data</a>
        <a href="/download/report" class="download-btn">📑 Download Report</a>
        <a href="/download/all" class="download-btn">📦 Download All Files</a>
      </div>
      <h3>Summary Preview:</h3>
      <pre style="white-space: pre-wrap; background: white; padding: 15px; border-radius: 5px; border: 1px solid #ddd; max-height: 400px; overflow-y: auto;">{{ summary }}</pre>
    </div>
  {% endif %}
</div>
<script>
function addUrl() {
  const container = document.getElementById('urlInputs');
  const newRow = document.createElement('div');
  newRow.className = 'url-row';
  newRow.innerHTML = `
    <input type="text" name="urls[]" placeholder="Enter landing page URL" required>
    <select name="types[]" required>
      <option value="">Select Type</option>
      <option value="client">Client (Your Site)</option>
      <option value="competitor">Competitor</option>
      <option value="manual">Manual Screenshot</option>
    </select>
    <button type="button" onclick="removeUrl(this)">Remove</button>
  `;
  container.appendChild(newRow);
}
function removeUrl(button) {
  const rows = document.querySelectorAll('.url-row');
  if (rows.length > 1) {
    button.parentElement.remove();
  }
}
function showLoading() {
  document.getElementById('loading').style.display = 'block';
  document.getElementById('analysisForm').style.display = 'none';
}
</script>
</body>
</html>
"""


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

def extract_site_name(url):
    try:
        domain_part = url.split("//")[-1].split("/")[0]
        parts = domain_part.split(".")
        base_name = parts[-2] if len(parts) >= 2 else parts[0]
        clean_name = base_name.lower().replace("-", "_").replace(".", "_")
        flushprint(f"Extracted site name '{clean_name}' from URL '{url}'")
        return clean_name
    except Exception as e:
        flushprint(f"extract_site_name error for URL '{url}': {e}")
        return "unknown"

def get_multimodal_analysis_from_gemini(page_content: str, image_bytes: bytes, provider_name: str, url: str, prompt_override=None) -> dict:
    flushprint(f"get_multimodal_analysis_from_gemini for {provider_name} at {url}")
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        pil_img = _prepare_image(Image.open(io.BytesIO(image_bytes)))

        prompt_text_section = f"""
- **Text Content (first 15,000 characters):**
---
{page_content[:15000]}
---""" if page_content else ""

        default_prompt = f"""
You are a digital marketing and CRO expert. Analyze this landing page screenshot and text content for '{provider_name}'.

**CRITICAL INSTRUCTIONS:**
1. You MUST respond with ONLY valid JSON - no other text, no explanations, no markdown
2. Do not include any text before or after the JSON
3. The JSON must be properly formatted and parseable

**Webpage Information**
- **Provider:** {provider_name}
- **URL:** {url}
{prompt_text_section}

Return ONLY this JSON structure (no other text):

{{
  "Platform": "{provider_name}",
  "URL": "{url}",
  "Main_Offer": "Describe the main value proposition or product offering",
  "Primary_CTA": "Primary call-to-action button text and placement",
  "Secondary_CTA": "Secondary call-to-action if visible",
  "Headline": "Main headline visible above the fold",
  "Subheadline": "Supporting headline or tagline",
  "Trust_Elements": "Trust signals like logos, testimonials, ratings, social proof",
  "Visual_Design": "Description of visual design, colors, layout style",
  "Above_Fold_Elements": "Key elements visible without scrolling",
  "Pricing_Info": "Any pricing information visible",
  "Course_Type": "Type of course, program, or service offered",
  "Target_Audience": "Apparent target audience based on messaging",
  "Unique_Selling_Points": "Key differentiators or unique features mentioned",
  "Lead_Generation_Type": "Type of conversion (direct purchase, free trial, lead gen, etc.)",
  "Form_Placement": "Position and type of forms visible",
  "Navigation_Style": "Description of navigation menu and structure",
  "Overall_Strategy": "Assessment of overall conversion strategy and approach"
}}"""

        prompt = prompt_override or default_prompt
        if prompt_override:
            # If custom prompt, ensure it asks for JSON format
            prompt = f"{prompt_override}\n\nIMPORTANT: Return your analysis ONLY as valid JSON with these fields: Platform, URL, Main_Offer, Primary_CTA, Secondary_CTA, Headline, Subheadline, Trust_Elements, Visual_Design, Above_Fold_Elements, Pricing_Info, Course_Type, Target_Audience, Unique_Selling_Points, Lead_Generation_Type, Form_Placement, Navigation_Style, Overall_Strategy"

        flushprint("Sending request to Gemini...")
        response = model.generate_content([prompt, pil_img])

        if not response.text or not response.text.strip():
            flushprint("Empty response – retrying without image")
            response = model.generate_content(prompt)

        flushprint(f"Received response (length: {len(response.text)})")
        
        try:
            result_dict = _extract_json(response.text)
            # Ensure required fields are present
            result_dict.update({"Platform": provider_name, "URL": url})
            flushprint("JSON parsed successfully")
            return result_dict
        except Exception as e:
            flushprint(f"JSON parse error: {e}")
            # Return a fallback structure with error info
            return {
                "Platform": provider_name,
                "URL": url,
                "Main_Offer": "Analysis failed - JSON parse error",
                "Primary_CTA": "N/A",
                "Secondary_CTA": "N/A", 
                "Headline": "N/A",
                "Subheadline": "N/A",
                "Trust_Elements": "N/A",
                "Visual_Design": "N/A",
                "Above_Fold_Elements": "N/A",
                "Pricing_Info": "N/A",
                "Course_Type": "N/A",
                "Target_Audience": "N/A",
                "Unique_Selling_Points": "N/A",
                "Lead_Generation_Type": "N/A",
                "Form_Placement": "N/A",
                "Navigation_Style": "N/A",
                "Overall_Strategy": "N/A",
                "error": f"JSON parse error: {str(e)}",
                "raw_response_snippet": response.text[:200] if response.text else "No response"
            }

    except Exception as e:
        flushprint("Gemini multimodal analysis failed:", e)
        return {
            "Platform": provider_name,
            "URL": url,
            "Main_Offer": "Analysis failed - API error",
            "Primary_CTA": "N/A",
            "Secondary_CTA": "N/A",
            "Headline": "N/A", 
            "Subheadline": "N/A",
            "Trust_Elements": "N/A",
            "Visual_Design": "N/A",
            "Above_Fold_Elements": "N/A",
            "Pricing_Info": "N/A",
            "Course_Type": "N/A",
            "Target_Audience": "N/A",
            "Unique_Selling_Points": "N/A",
            "Lead_Generation_Type": "N/A",
            "Form_Placement": "N/A",
            "Navigation_Style": "N/A",
            "Overall_Strategy": "N/A",
            "error": f"Gemini API error: {str(e)}"
        }


def generate_summary_report(course_data_df: pd.DataFrame, client_name: str) -> str:
    flushprint(f"Generating summary report with client: {client_name}")
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        
        data_string = course_data_df.to_csv(index=False)
        
        client_row = course_data_df[course_data_df['Platform'].str.lower() == client_name.lower()]
        client_url = client_row['URL'].iloc[0] if not client_row.empty else "Not specified"
        
        competitor_info = []
        for _, row in course_data_df.iterrows():
            if row['Platform'].lower() != client_name.lower():
                competitor_info.append(f"- {row['Platform']}: {row['URL']}")
        
        competitor_urls = "\n".join(competitor_info)

        prompt = f"""
        You are a CRO and digital strategy expert. Analyze the landing page performance of '{client_name}' compared to its competitors.

        📌 **Client Landing Page:** {client_url}
        **Client Name:** {client_name}

        **Competitor Landing Pages:**
        {competitor_urls}

        **Detailed Comparison Data (CSV):**
        ```csv
        {data_string}
        ```

        🎯 **Your Task:**
        1. Write a **Strategic Executive Summary** for {client_name} identifying key conversion optimization opportunities
        2. Create a **Competitive Analysis Overview** highlighting how {client_name} compares to competitors
        3. Provide a **Priority Opportunities Table** with:
           - Opportunity
           - Impact Level (High/Medium/Low)
           - Implementation Difficulty (Easy/Medium/Hard)
           - Rationale (competitor insight or CRO best practice)
           - Specific Tactical Recommendations
        4. Conclude with **Strategic Recommendations** for immediate action

        **Focus Areas:**
        - Above-the-fold optimization
        - CTA placement and messaging
        - Trust signal improvements
        - Value proposition clarity
        - User experience enhancements
        - Conversion funnel optimization

        Present your response in clean, professional markdown format suitable for executive presentation.
        """

        response = model.generate_content(prompt)
        return response.text
        
    except Exception as e:
        flushprint(f"Failed to generate summary report: {e}")
        return f"Error generating summary report: {str(e)}"

# -- Main analyzer function --
def analyze_landing_pages(landing_pages, prompt_override=None):
    flushprint("analyze_landing_pages called")
    all_course_data = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_id = f"session_{timestamp}"
    session_dir = os.path.join(app.config['OUTPUT_FOLDER'], f"analysis_{timestamp}")
    os.makedirs(session_dir, exist_ok=True)
    
    client_name = None
    
    # Identify client
    for lp in landing_pages:
        if lp.get('type') == 'client':
            client_name = lp['name']
            break
    
    if not client_name:
        client_name = landing_pages[0]['name'] if landing_pages else "Unknown"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36'
            )

            for lp in landing_pages:
                flushprint(f"Processing {lp['name']} ({lp['url']}) type={lp.get('type')} manual={lp.get('manual')}")
                
                if lp.get("manual", False) or lp.get('type') == 'manual':
                    # MANUAL SCREENSHOT PROCESSING
                    manual_file = f"{lp['name']}_manual.png"
                    manual_path = os.path.join(app.config['UPLOAD_FOLDER'], manual_file)
                    
                    if not os.path.exists(manual_path):
                        flushprint(f"Manual screenshot not found: {manual_file}")
                        all_course_data.append({
                            "Platform": lp['name'], 
                            "URL": lp['url'],
                            "Type": lp.get('type', 'unknown'),
                            "error": f"Manual screenshot '{manual_file}' not found."
                        })
                        continue
                    
                    try:
                        with open(manual_path, "rb") as f:
                            image_bytes = f.read()
                        
                        # Save screenshot to database
                        save_screenshot_to_db(lp['name'], lp['url'], image_bytes)
                        
                        page_content = ""
                        structured_data = get_multimodal_analysis_from_gemini(
                            page_content, image_bytes, lp['name'], lp['url'], prompt_override
                        )
                        structured_data['Type'] = lp.get('type', 'manual')
                        all_course_data.append(structured_data)
                        flushprint(f"Manual analysis completed for {lp['name']}")
                    except Exception as e:
                        flushprint(f"Error processing manual screenshot for {lp['name']}: {e}")
                        all_course_data.append({
                            "Platform": lp['name'], 
                            "URL": lp['url'],
                            "Type": lp.get('type', 'unknown'),
                            "error": f"Manual processing error: {str(e)}"
                        })
                else:
                    # AUTOMATIC SCREENSHOT PROCESSING
                    page = context.new_page()
                    try:
                        flushprint(f"Navigating to {lp['url']}")
                        page.goto(lp["url"], wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(5000)
                        
                        screenshot_path = os.path.join(session_dir, f"{lp['name']}_fullpage.png")
                        page.screenshot(path=screenshot_path, full_page=True)
                        
                        with open(screenshot_path, "rb") as f:
                            image_bytes = f.read()
                        
                        # Save screenshot to database
                        save_screenshot_to_db(lp['name'], lp['url'], image_bytes)
                        
                        page_content = page.inner_text('body')
                        structured_data = get_multimodal_analysis_from_gemini(
                            page_content, image_bytes, lp['name'], lp['url'], prompt_override
                        )
                        structured_data['Type'] = lp.get('type', 'competitor')
                        all_course_data.append(structured_data)
                        flushprint(f"Automatic analysis completed for {lp['name']}")
                        
                    except PlaywrightTimeoutError:
                        flushprint(f"Timeout error for {lp['name']}")
                        all_course_data.append({
                            "Platform": lp['name'], 
                            "URL": lp['url'],
                            "Type": lp.get('type', 'unknown'),
                            "error": "Page load timeout"
                        })
                    except Exception as e:
                        flushprint(f"Error for auto processing {lp['name']}: {e}")
                        all_course_data.append({
                            "Platform": lp['name'], 
                            "URL": lp['url'],
                            "Type": lp.get('type', 'unknown'),
                            "error": f"Auto processing error: {str(e)}"
                        })
                    finally:
                        page.close()
                        
            browser.close()
            flushprint("Browser closed")
            
    except Exception as e:
        flushprint("Fatal error in analyze_landing_pages:", e)
        all_course_data.append({
            "Platform": "SYSTEM_ERROR",
            "URL": "N/A",
            "Type": "error", 
            "error": f"Fatal error: {str(e)}"
        })

    if not all_course_data:
        all_course_data.append({
            "Platform": "NO_DATA",
            "URL": "N/A",
            "Type": "error",
            "error": "No data was collected"
        })

    # Save CSV (local and database)
    df = pd.DataFrame(all_course_data)
    csv_path = os.path.join(session_dir, "competitive_analysis_data.csv")
    df.to_csv(csv_path, index=False)
    flushprint(f"CSV saved locally with {len(all_course_data)} records")
    
    # Save CSV to database
    with open(csv_path, 'rb') as f:
        csv_bytes = f.read()
    save_analysis_result_to_db(session_id, 'csv', 'competitive_analysis_data.csv', 
                               content=df.to_csv(index=False), file_data=csv_bytes, client_name=client_name)
    
    # Generate summary report
    successful_df = df[df['error'].isnull()].copy() if 'error' in df.columns else df
    
    if not successful_df.empty:
        summary_report = generate_summary_report(successful_df, client_name)
        report_path = os.path.join(session_dir, "summary_and_recommendations.md")
        with open(report_path, "w", encoding='utf-8') as f:
            f.write(summary_report)
        flushprint(f"Summary report saved locally to {report_path}")
        
        # Save report to database
        with open(report_path, 'rb') as f:
            report_bytes = f.read()
        save_analysis_result_to_db(session_id, 'report', 'summary_and_recommendations.md', 
                                   content=summary_report, file_data=report_bytes, client_name=client_name)
    else:
        summary_report = "No successful data collection for report generation."
    
    # Store session info for downloads
    app.config['LAST_SESSION_ID'] = session_id
    app.config['LAST_CSV_PATH'] = csv_path
    app.config['LAST_REPORT_PATH'] = report_path if 'report_path' in locals() else None
    app.config['LAST_SESSION_DIR'] = session_dir
    
    return summary_report, csv_path

# --- Routes ---
@app.route("/", methods=["GET", "POST"])
def index():
    flushprint("Index route called:", request.method)
    summary = None
    error = None
    success = None
    prompt = ''
    default_prompt = """
    Analyze this landing page and provide comprehensive insights about:
    1. Value proposition and messaging clarity
    2. Call-to-action effectiveness and placement
    3. Trust elements and social proof
    4. Visual design and user experience
    5. Conversion optimization opportunities
    6. Competitive positioning elements
    """

    if request.method == "POST":
        try:
            urls = request.form.getlist("urls[]")
            types = request.form.getlist("types[]")
            prompt = request.form.get("prompt", "")
            
            # Save manual screenshots
            uploaded_files = request.files
            uploaded_names = save_manual_screenshots(uploaded_files)
            flushprint(f"Manual screenshots saved: {uploaded_names}")
            
            # Validate inputs
            if not urls or not any(url.strip() for url in urls):
                error = "Please provide at least one URL"
                return render_template_string(HTML, error=error, prompt=prompt, default_prompt=default_prompt)
            
            if len(urls) != len(types):
                error = "Each URL must have a corresponding type selected"
                return render_template_string(HTML, error=error, prompt=prompt, default_prompt=default_prompt)
            
            # Check for at least one client
            if 'client' not in types:
                error = "Please designate at least one URL as 'Client'"
                return render_template_string(HTML, error=error, prompt=prompt, default_prompt=default_prompt)
            
            # Prepare landing pages
            landing_pages = []
            for url, url_type in zip(urls, types):
                if url.strip() and url_type:
                    name = extract_site_name(url)
                    
                    # Check if manual screenshot exists
                    manual_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{name}_manual.png")
                    is_manual = os.path.exists(manual_path) or url_type == 'manual'
                    
                    landing_pages.append({
                        "name": name,
                        "url": url.strip(),
                        "type": url_type,
                        "manual": is_manual
                    })
            
            flushprint("Landing pages prepared:", landing_pages)
            
            if not landing_pages:
                error = "No valid URLs to analyze"
                return render_template_string(HTML, error=error, prompt=prompt, default_prompt=default_prompt)
            
            # Run analysis
            summary, csv_path = analyze_landing_pages(landing_pages, prompt if prompt.strip() else None)
            success = f"Analysis completed successfully! Processed {len(landing_pages)} landing pages. Data saved to database."
            
        except Exception as e:
            error = f"Analysis failed: {str(e)}"
            flushprint("Error in analysis:", e)

    return render_template_string(HTML, summary=summary, error=error, success=success, prompt=prompt, default_prompt=default_prompt)

@app.route('/download/csv')
def download_csv():
    flushprint("Download CSV requested")
    
    # Try to get from database first
    session_id = app.config.get('LAST_SESSION_ID')
    if session_id:
        result = get_analysis_result_from_db(session_id, 'csv')
        if result:
            return send_file(
                io.BytesIO(result['file_data']), 
                as_attachment=True, 
                download_name=result['file_name'],
                mimetype='text/csv'
            )
    
    # Fallback to local file
    path = app.config.get('LAST_CSV_PATH')
    if not path or not os.path.exists(path):
        return "No CSV file available. Please run an analysis first.", 404
    return send_file(path, as_attachment=True, download_name="competitive_analysis_data.csv")

@app.route('/download/report')
def download_report():
    flushprint("Download report requested")
    
    # Try to get from database first
    session_id = app.config.get('LAST_SESSION_ID')
    if session_id:
        result = get_analysis_result_from_db(session_id, 'report')
        if result:
            return send_file(
                io.BytesIO(result['file_data']), 
                as_attachment=True, 
                download_name=result['file_name'],
                mimetype='text/markdown'
            )
    
    # Fallback to local file
    path = app.config.get('LAST_REPORT_PATH')
    if not path or not os.path.exists(path):
        return "No report file available. Please run an analysis first.", 404
    return send_file(path, as_attachment=True, download_name="summary_and_recommendations.md")

@app.route('/download/all')
def download_all():
    flushprint("Download all files requested")
    session_dir = app.config.get('LAST_SESSION_DIR')
    if not session_dir or not os.path.exists(session_dir):
        return "No analysis files available. Please run an analysis first.", 404
    
    # Create zip file
    zip_path = os.path.join(session_dir, "complete_analysis.zip")
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for root, dirs, files in os.walk(session_dir):
            for file in files:
                if file != "complete_analysis.zip":  # Don't include the zip itself
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, session_dir)
                    zipf.write(file_path, arcname)
    
    return send_file(zip_path, as_attachment=True, download_name="complete_analysis.zip")

@app.route("/ping")
def ping():
    return "pong"

@app.route('/check-env')
def check_env():
    """Check environment variables for debugging"""
    db_url = os.environ.get("DATABASE_URL", "Not set")
    gemini_key = os.environ.get("GEMINI_API_KEY", "Not set")
    
    # Mask sensitive data
    masked_db_url = "Not set"
    if db_url != "Not set":
        if "@" in db_url and "://" in db_url:
            parts = db_url.split("://")
            if len(parts) > 1:
                protocol = parts[0]
                rest = parts[1]
                if "@" in rest:
                    auth_and_host = rest.split("@")
                    if len(auth_and_host) > 1:
                        masked_db_url = f"{protocol}://***:***@{auth_and_host[1]}"
                else:
                    masked_db_url = db_url
        else:
            masked_db_url = db_url[:20] + "..." if len(db_url) > 20 else db_url
    
    masked_gemini = "Set" if gemini_key != "Not set" else "Not set"
    
    return {
        "DATABASE_URL": masked_db_url,
        "GEMINI_API_KEY": masked_gemini,
        "url_type": "external" if "render.com" in db_url else ("internal" if "dpg-" in db_url else "unknown"),
        "ssl_in_url": "sslmode=" in db_url if db_url != "Not set" else False
    }

@app.route('/test-db')
def test_db():
    """Test database connection for debugging"""
    try:
        db_url = os.environ.get("DATABASE_URL", "Not set")
        
        # Mask sensitive parts of URL for logging
        masked_url = db_url
        if "@" in db_url and "://" in db_url:
            parts = db_url.split("://")
            if len(parts) > 1:
                protocol = parts[0]
                rest = parts[1]
                if "@" in rest:
                    auth_and_host = rest.split("@")
                    if len(auth_and_host) > 1:
                        masked_url = f"{protocol}://***:***@{auth_and_host[1]}"
        
        print(f"Testing connection with URL: {masked_url}")
        
        # Check if URL contains internal vs external format
        url_type = "unknown"
        if "render.com" in db_url:
            url_type = "external"
        elif "dpg-" in db_url and "render.com" not in db_url:
            url_type = "internal"
        
        # Test connection details
        connection_method = "unknown"
        ssl_mode = "unknown"
        
        # Parse URL to check SSL mode
        if "sslmode=" in db_url:
            ssl_mode = db_url.split("sslmode=")[1].split("&")[0]
        
        conn = get_db_conn()
        
        # Check connection info
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version();")
                result = cur.fetchone()
                cur.execute("SELECT current_database();")
                db_name = cur.fetchone()
                
                # Get connection info
                cur.execute("SHOW ssl;")
                ssl_status = cur.fetchone()
                
        conn.close()
        
        return {
            "status": "success",
            "message": "Database connected successfully!",
            "url_type": url_type,
            "ssl_enabled": ssl_status['ssl'] if ssl_status else "unknown",
            "database": db_name['current_database'] if db_name else "unknown",
            "version": result['version'][:100] + "..." if result else "unknown",
            "masked_url": masked_url
        }
    except Exception as e:
        error_msg = str(e)
        print(f"Database connection test failed: {error_msg}")
        
        # Provide specific guidance based on error type
        guidance = ""
        if "Name or service not known" in error_msg:
            guidance = "DNS resolution failed. Check if database and web service are in same region, or use External Database URL."
        elif "SSL connection has been closed unexpectedly" in error_msg:
            guidance = "SSL handshake failed. The new connection method should try multiple SSL modes automatically."
        elif "SSL" in error_msg:
            guidance = "SSL connection issue. Try adding ?sslmode=prefer to DATABASE_URL."
        elif "authentication failed" in error_msg:
            guidance = "Wrong username/password. Check DATABASE_URL credentials."
        elif "timeout" in error_msg.lower():
            guidance = "Connection timeout. Database may be sleeping or overloaded."
        
        return {
            "status": "error", 
            "message": f"Database connection failed: {error_msg}",
            "guidance": guidance,
            "url_format": "Using: " + (masked_url if 'masked_url' in locals() else "URL not available")
        }, 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flushprint(f"Starting Dynamic Landing Page Analyzer on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)
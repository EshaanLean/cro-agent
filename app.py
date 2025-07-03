import os
import io
import json
import sys
import pandas as pd
from flask import Flask, request, render_template_string, send_file, jsonify
import zipfile
import tldextract
from datetime import datetime

# --- Flushed print utility ---
def flushprint(*args, **kwargs):
    print(*args, **kwargs)
    sys.stdout.flush()

# ------------- LOG APP STARTUP -------------
flushprint("=== Dynamic Landing Page Analyzer starting up ===")

try:
    import google.generativeai as genai
    from PIL import Image
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    flushprint("Imports successful")
except Exception as e:
    flushprint("Import error:", e)
    raise

# -- API KEY SETUP --
API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    flushprint("ERROR: GEMINI_API_KEY env var not set!")
    raise ValueError("GEMINI_API_KEY env var not set! Add it on your hosting platform.")
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
app.config['OUTPUT_FOLDER'] = "analysis_results"
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

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
  </style>
</head>
<body>
<div class="container">
  <h1>🚀 Dynamic Landing Page Analyzer</h1>
  
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

# -- Utility functions --
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
    """Extracts the main domain (e.g., 'udemy' from 'www.udemy.com', 'example' from 'example.co.uk')."""
    try:
        ext = tldextract.extract(url)
        # ext.domain is the main part of the domain
        clean_name = ext.domain.lower().replace("-", "_").replace(".", "_")
        flushprint(f"Extracted site name '{clean_name}' from URL '{url}'")
        return clean_name
    except Exception as e:
        flushprint(f"extract_site_name error for URL '{url}': {e}")
        return "unknown"



# -- Gemini Analysis Function --
def get_multimodal_analysis_from_gemini(page_content: str, image_bytes: bytes, provider_name: str, url: str, prompt_override=None) -> dict:
    flushprint(f"get_multimodal_analysis_from_gemini for {provider_name} at {url}")
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        image = Image.open(io.BytesIO(image_bytes))

        MAX_PIXELS = 16383
        if image.height > MAX_PIXELS:
            aspect_ratio = image.width / image.height
            new_height = MAX_PIXELS - 1
            new_width = int(new_height * aspect_ratio)
            image = image.resize((new_width, new_height), Image.LANCZOS)
            flushprint("Image resized for Gemini API")

        prompt_text_section = f"""
        - **Text Content (first 15,000 characters):**
        ---
        {page_content[:15000]}
        ---
        """ if page_content else ""

        default_analysis_prompt = f"""
        As a digital marketing and CRO expert, analyze this landing page screenshot and text content for '{provider_name}'.
        
        **Webpage Information:**
        - **Provider:** {provider_name}
        - **URL:** {url}
        {prompt_text_section}

        Please provide a detailed analysis in JSON format with the following structure:
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
        }}

        Return only valid JSON, no additional text or markdown formatting.
        """

        prompt = prompt_override or default_analysis_prompt
        
        response = model.generate_content([prompt, image])
        
        if not response.text:
            raise Exception("Empty response from Gemini API")
            
        cleaned_json = response.text.strip().replace("```json", "").replace("```", "")
        
        try:
            result = json.loads(cleaned_json)
            result["Platform"] = provider_name
            result["URL"] = url
            flushprint("JSON parsed successfully")
            return result
        except json.JSONDecodeError as e:
            flushprint(f"JSON parse error: {e}")
            return {
                "Platform": provider_name,
                "URL": url,
                "error": f"JSON parse error: {str(e)}",
                "raw_response": cleaned_json[:500]
            }
            
    except Exception as e:
        flushprint("Gemini multimodal analysis failed:", e)
        return {
            "Platform": provider_name,
            "URL": url,
            "error": f"Gemini API error: {str(e)}"
        }

# -- Generate Summary Report --
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

    # Save CSV
    df = pd.DataFrame(all_course_data)
    csv_path = os.path.join(session_dir, "competitive_analysis_data.csv")
    df.to_csv(csv_path, index=False)
    flushprint(f"CSV saved with {len(all_course_data)} records")
    
    # Generate summary report
    successful_df = df[df['error'].isnull()].copy() if 'error' in df.columns else df
    
    if not successful_df.empty:
        summary_report = generate_summary_report(successful_df, client_name)
        report_path = os.path.join(session_dir, "summary_and_recommendations.md")
        with open(report_path, "w", encoding='utf-8') as f:
            f.write(summary_report)
        flushprint(f"Summary report saved to {report_path}")
    else:
        summary_report = "No successful data collection for report generation."
    
    # Store paths for downloads
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
            success = f"Analysis completed successfully! Processed {len(landing_pages)} landing pages."
            
        except Exception as e:
            error = f"Analysis failed: {str(e)}"
            flushprint("Error in analysis:", e)

    return render_template_string(HTML, summary=summary, error=error, success=success, prompt=prompt, default_prompt=default_prompt)

@app.route('/download/csv')
def download_csv():
    flushprint("Download CSV requested")
    path = app.config.get('LAST_CSV_PATH')
    if not path or not os.path.exists(path):
        return "No CSV file available. Please run an analysis first.", 404
    return send_file(path, as_attachment=True, download_name="competitive_analysis_data.csv")

@app.route('/download/report')
def download_report():
    flushprint("Download report requested")
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flushprint(f"Starting Dynamic Landing Page Analyzer on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)
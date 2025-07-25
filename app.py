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

        # Set the SSL mode to 'prefer'
        query_params['sslmode'] = ['prefer'] # Use 'prefer' instead of 'require'

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
                    print("Screenshots table ready")
                    
                    # Create landing_page_analysis table (NEW)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS landing_page_analysis (
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
                    print("Landing page analysis table ready")
                    
                    # Check if the old analysis_results table exists and log its structure
                    cur.execute("""
                        SELECT column_name, data_type 
                        FROM information_schema.columns 
                        WHERE table_name = 'analysis_results'
                        ORDER BY ordinal_position
                        LIMIT 5;
                    """)
                    existing_columns = cur.fetchall()
                    
                    if existing_columns:
                        print("Note: Found existing analysis_results table with different structure")
                        print("Using new landing_page_analysis table for this app")
                    
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

# Default prompts as global variables
DEFAULT_ANALYSIS_PROMPT = """Analyze this landing page and provide comprehensive insights about:
1. Value proposition and messaging clarity
2. Call-to-action effectiveness and placement
3. Trust elements and social proof
4. Visual design and user experience
5. Conversion optimization opportunities
6. Competitive positioning elements
7. Identify ALL sections on the page (both above and below the fold)
   - Group similar sections into general categories to enable meaningful comparison
   - Capture specific implementation details separately
8. Compare section presence across competitors to identify gaps and opportunities"""

DEFAULT_STRUCTURED_PROMPT_TEMPLATE = """Analyze this landing page screenshot for {provider_name} ({url}).

INSTRUCTIONS:
1. Examine the ENTIRE page - both above and below the fold
2. Identify ALL sections and elements present on the page
3. Group similar sections into general categories while preserving specific details
4. Return ONLY a JSON object with the exact structure shown below
5. No explanations, no markdown formatting, just the JSON

{text_content_section}

You MUST return this EXACT JSON structure:

{{
  "Platform": "{provider_name}",
  "URL": "{url}",
  "Main_Offer": "[describe the main product/service offered]",
  "Primary_CTA": "[primary call-to-action text and location]",
  "Secondary_CTA": "[secondary CTA if present, or 'None']",
  "Headline": "[main headline text]",
  "Subheadline": "[subheadline or tagline text]",
  "Trust_Elements": "[list trust signals like logos, testimonials, ratings]",
  "Visual_Design": "[describe design, colors, layout style]",
  "Above_Fold_Elements": "[list key elements visible without scrolling]",
  "Pricing_Info": "[pricing details if shown, or 'Not visible']",
  "Target_Audience": "[identified target audience]",
  "Unique_Selling_Points": "[key differentiators mentioned]",
  "Lead_Generation_Type": "[type: email signup, free trial, purchase, etc.]",
  "Above_Fold_Sections": [
    "Hero Section",
    "Navigation Bar",
    "[Add other sections you identify above the fold]"
  ],
  "Below_Fold_Sections": [
    "[Group similar sections into categories:]",
    "Features/Benefits Section",
    "Testimonials/Reviews",
    "Pricing Information",
    "FAQ Section",
    "[Add all other sections you identify below the fold]",
    "[Be descriptive but use general categories when possible]"
  ],
  "Section_Details": {{
    "[For each general category, provide specific implementation details]",
    "Features/Benefits Section": "Interactive grid showing 6 key platform features with animations",
    "Testimonials/Reviews": "Video testimonials carousel with 12 student success stories",
    "[Add details for each section category you identified]"
  }}
}}

IMPORTANT: 
- Above_Fold_Sections and Below_Fold_Sections should be ARRAYS of section names (strings)
- Use general category names in the arrays when multiple similar elements exist
- Provide specific implementation details in Section_Details
- Be comprehensive - include every distinct section you can identify"""

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
    """Save analysis results to the landing_page_analysis table"""
    try:
        conn = get_db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Use the new landing_page_analysis table
                    cur.execute("""
                        INSERT INTO landing_page_analysis 
                        (session_id, file_type, file_name, content, file_data, client_name) 
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        session_id, 
                        file_type, 
                        file_name, 
                        content, 
                        psycopg2.Binary(file_data) if file_data else None, 
                        client_name
                    ))
                    print(f"Saved {file_type} to landing_page_analysis table: {file_name}")
        finally:
            conn.close()
    except Exception as e:
        print(f"Error saving {file_type} to database: {e}")
        # If the new table doesn't exist, try the original approach
        if "landing_page_analysis" in str(e):
            print("Landing page analysis table doesn't exist. Please visit /create-landing-page-tables")

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
    """Retrieve analysis results from the landing_page_analysis table"""
    try:
        conn = get_db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Try the new table first
                    try:
                        cur.execute("""
                            SELECT file_name, content, file_data 
                            FROM landing_page_analysis 
                            WHERE session_id=%s AND file_type=%s 
                            ORDER BY created_at DESC LIMIT 1
                        """, (session_id, file_type))
                        
                        row = cur.fetchone()
                        if row:
                            return {
                                'file_name': row['file_name'],
                                'content': row['content'],
                                'file_data': row['file_data']
                            }
                    except Exception as e:
                        if "landing_page_analysis" in str(e):
                            print("Landing page analysis table doesn't exist")
                            return None
                        raise
                        
        finally:
            conn.close()
    except Exception as e:
        print(f"Error retrieving analysis result from database: {e}")
        return None

@app.route('/screenshot/<n>')
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
    """List recent analysis results from the landing_page_analysis table"""
    try:
        conn = get_db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Check if landing_page_analysis table exists
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT FROM information_schema.tables 
                            WHERE table_name = 'landing_page_analysis'
                        )
                    """)
                    table_exists = cur.fetchone()['exists']
                    
                    if table_exists:
                        # Use the new table
                        cur.execute("""
                            SELECT session_id, file_type, file_name, client_name, created_at 
                            FROM landing_page_analysis 
                            ORDER BY created_at DESC LIMIT 50
                        """)
                        rows = cur.fetchall()
                        return jsonify({
                            "table": "landing_page_analysis",
                            "results": rows
                        })
                    else:
                        # Fallback to old table structure
                        cur.execute("""
                            SELECT session_id, file_type, file_name, client_name, created_at 
                            FROM analysis_results 
                            WHERE file_type IS NOT NULL
                            ORDER BY created_at DESC LIMIT 50
                        """)
                        rows = cur.fetchall()
                        return jsonify({
                            "table": "analysis_results (old)",
                            "message": "Please create new tables by visiting /create-landing-page-tables",
                            "results": rows
                        })
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
    .container { max-width: 1400px; margin: auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
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
    .results-container { margin-top: 30px; }
    .table-section { margin-bottom: 30px; }
    .table-section h3 { color: #2c3e50; margin-bottom: 15px; border-bottom: 2px solid #007bff; padding-bottom: 10px; }
    .data-table { width: 100%; border-collapse: collapse; background: white; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
    .data-table th { background: #007bff; color: white; padding: 12px; text-align: left; font-weight: 600; }
    .data-table td { padding: 10px; border-bottom: 1px solid #eee; }
    .data-table tr:hover { background: #f8f9fa; }
    .data-table tr:nth-child(even) { background: #f8f9fa; }
    .client-row { background: #e3f2fd !important; font-weight: 500; }
    .section-table { width: 100%; border-collapse: collapse; background: white; }
    .section-table th { background: #343a40; color: white; padding: 12px; text-align: center; position: sticky; top: 0; z-index: 10; }
    .section-table td { padding: 8px; border: 1px solid #dee2e6; text-align: center; }
    .section-table .section-name { text-align: left; font-weight: 500; background: #f8f9fa; }
    .section-table .separator { background: #e9ecef; font-weight: bold; color: #495057; }
    .section-table .yes { color: #28a745; font-size: 20px; }
    .section-table .no { color: #dc3545; font-size: 20px; }
    .table-wrapper { overflow-x: auto; max-height: 600px; overflow-y: auto; border: 1px solid #dee2e6; border-radius: 5px; }
    .tabs { display: flex; gap: 10px; margin-bottom: 20px; }
    .tab { padding: 10px 20px; background: #e9ecef; border: none; border-radius: 5px 5px 0 0; cursor: pointer; font-weight: 500; }
    .tab.active { background: #007bff; color: white; }
    .tab-content { display: none; }
    .tab-content.active { display: block; }
    .insights-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 15px; margin-bottom: 20px; }
    .insight-card { background: white; padding: 15px; border-radius: 8px; border: 1px solid #dee2e6; }
    .insight-card h4 { color: #007bff; margin-top: 0; }
    .insight-card .value { font-size: 24px; font-weight: bold; color: #2c3e50; }
    .expandable { cursor: pointer; }
    .expandable:hover { background: #e3f2fd !important; }
    .detail-row { display: none; }
    .detail-content { padding: 15px; background: #f8f9fa; border-left: 4px solid #007bff; }
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
    <a href="/create-landing-page-tables" class="debug-btn" target="_blank" style="background: #ff5722;">Create Landing Page Tables</a>
    <a href="/migrate-db" class="debug-btn" target="_blank">Migrate Database</a>
    <a href="/screenshots" class="debug-btn" target="_blank">View Screenshots DB</a>
    <a href="/analysis_results" class="debug-btn" target="_blank">View Analysis Results DB</a>
    <a href="/debug/last-analysis" class="debug-btn" target="_blank">Debug Last Analysis</a>
    <p><small>If you're getting database errors, click "Create Landing Page Tables" first!</small></p>
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
  {% if analysis_data %}
    <div class="result">
      <h2>📊 Analysis Complete!</h2>
      <!-- Download Section -->
      <div class="download-section">
        <h3>Download Results:</h3>
        <a href="/download/csv" class="download-btn">📄 Download CSV Data</a>
        <a href="/download/sections" class="download-btn">📊 Download Section Comparison</a>
        <a href="/download/report" class="download-btn">📑 Download Report</a>
        <a href="/download/all" class="download-btn">📦 Download All Files</a>
      </div>
      <!-- Results Display Section -->
      <div class="results-container">
        <!-- Tabs -->
        <div class="tabs">
          <button class="tab active" onclick="showTab('overview')">📈 Overview</button>
          <button class="tab" onclick="showTab('analysis')">🔍 Detailed Analysis</button>
          <button class="tab" onclick="showTab('sections')">📋 Section Comparison</button>
          <button class="tab" onclick="showTab('report')">📑 Summary Report</button>
        </div>
        <!-- Overview Tab -->
        <div id="overview" class="tab-content active">
          <div class="table-section">
            <h3>Key Insights</h3>
            <div class="insights-grid">
              <div class="insight-card">
                <h4>Total Sites Analyzed</h4>
                <div class="value">{{ analysis_data|length }}</div>
              </div>
              <div class="insight-card">
                <h4>Client</h4>
                <div class="value">{{ client_name }}</div>
              </div>
              <div class="insight-card">
                <h4>Competitors</h4>
                <div class="value">{{ analysis_data|length - 1 }}</div>
              </div>
              <div class="insight-card">
                <h4>Unique Sections Found</h4>
                <div class="value">{{ total_sections }}</div>
              </div>
            </div>
            <h3>Quick Comparison</h3>
            <div class="table-wrapper">
              <table class="data-table">
                <thead>
                  <tr>
                    <th>Platform</th>
                    <th>Type</th>
                    <th>Main Offer</th>
                    <th>Primary CTA</th>
                    <th>Trust Elements</th>
                    <th>Lead Gen Type</th>
                  </tr>
                </thead>
                <tbody>
                  {% for row in analysis_data %}
                  <tr class="{% if row.Type == 'client' %}client-row{% endif %}">
                    <td><strong>{{ row.Platform }}</strong></td>
                    <td>{{ row.Type }}</td>
                    <td>{{ (row.Main_Offer|string)[:100] }}{% if (row.Main_Offer|string)|length > 100 %}...{% endif %}</td>
                    <td>{{ row.Primary_CTA }}</td>
                    <td>{{ (row.Trust_Elements|string)[:80] }}{% if (row.Trust_Elements|string)|length > 80 %}...{% endif %}</td>
                    <td>{{ row.Lead_Generation_Type }}</td>
                  </tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </div>
        </div>
        <!-- Detailed Analysis Tab -->
        <div id="analysis" class="tab-content">
          <div class="table-section">
            <h3>Complete Analysis Data</h3>
            <div class="table-wrapper">
              <table class="data-table">
                <thead>
                  <tr>
                    {% for col in analysis_columns %}
                    <th>{{ col }}</th>
                    {% endfor %}
                  </tr>
                </thead>
                <tbody>
                  {% for row in analysis_data %}
                  <tr class="expandable" onclick="toggleDetail('detail-{{ loop.index }}')">
                    {% for col in analysis_columns %}
                    <td>
                      {% if col in ['Above_Fold_Sections', 'Below_Fold_Sections'] %}
                        <em>Click to expand</em>
                      {% else %}
                        {% set val = row[col]|string %}
                        {{ val[:100] if val|length > 100 else val }}
                      {% endif %}
                    </td>
                    {% endfor %}
                  </tr>
                  <tr id="detail-{{ loop.index }}" class="detail-row">
                    <td colspan="{{ analysis_columns|length }}">
                      <div class="detail-content">
                        <h4>Complete Details for {{ row.Platform }}</h4>
                        {% for col in analysis_columns %}
                        <p><strong>{{ col }}:</strong> {{ row[col] }}</p>
                        {% endfor %}
                      </div>
                    </td>
                  </tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </div>
        </div>
        <!-- Section Comparison Tab -->
        <div id="sections" class="tab-content">
          <div class="table-section">
            <h3>Section Presence Comparison</h3>
            <div class="table-wrapper">
              <table class="section-table">
                <thead>
                  <tr>
                    <th style="text-align: left;">Section</th>
                    {% for col in section_columns[1:] %}
                    <th>{{ col }}</th>
                    {% endfor %}
                  </tr>
                </thead>
                <tbody>
                  {% for row in section_data %}
                  <tr class="{% if '===' in row.Section %}separator{% endif %}">
                    <td class="section-name">{{ row.Section }}</td>
                    {% for col in section_columns[1:] %}
                    <td>
                      {% if row[col] == '✅' %}
                        <span class="yes">✅</span>
                      {% elif row[col] == '❌' %}
                        <span class="no">❌</span>
                      {% else %}
                        {{ row[col] }}
                      {% endif %}
                    </td>
                    {% endfor %}
                  </tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </div>
        </div>
        <!-- Summary Report Tab -->
        <div id="report" class="tab-content">
          <div class="table-section">
            <h3>Summary Report</h3>
            <pre style="white-space: pre-wrap; background: white; padding: 15px; border-radius: 5px; border: 1px solid #ddd; max-height: 600px; overflow-y: auto;">{{ summary }}</pre>
          </div>
        </div>
      </div>
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
function showTab(tabName) {
  // Hide all tabs
  const tabs = document.querySelectorAll('.tab-content');
  tabs.forEach(tab => tab.classList.remove('active'));
  // Remove active class from all tab buttons
  const tabButtons = document.querySelectorAll('.tab');
  tabButtons.forEach(btn => btn.classList.remove('active'));
  // Show selected tab
  document.getElementById(tabName).classList.add('active');
  // Add active class to clicked button
  event.target.classList.add('active');
}
function toggleDetail(rowId) {
  const detailRow = document.getElementById(rowId);
  if (detailRow.style.display === 'table-row') {
    detailRow.style.display = 'none';
  } else {
    detailRow.style.display = 'table-row';
  }
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

def get_multimodal_analysis_from_gemini(page_content: str, image_bytes: bytes, provider_name: str, url: str, prompt_override=None, structured_prompt_override=None, all_providers=None) -> dict:
    flushprint(f"get_multimodal_analysis_from_gemini for {provider_name} at {url}")
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        pil_img = _prepare_image(Image.open(io.BytesIO(image_bytes)))

        # Text content section
        prompt_text_section = f"""
Text Content Preview (first 5000 chars):
{page_content[:5000] if page_content else "No text content available"}
""" 

        # Use the structured prompt template (either default or override)
        if structured_prompt_override and structured_prompt_override.strip():
            structured_prompt = structured_prompt_override.format(
                provider_name=provider_name,
                url=url,
                text_content_section=prompt_text_section
            )
        else:
            structured_prompt = DEFAULT_STRUCTURED_PROMPT_TEMPLATE.format(
                provider_name=provider_name,
                url=url,
                text_content_section=prompt_text_section
            )

        # If there's a custom analysis prompt, append it
        if prompt_override and prompt_override.strip():
            final_prompt = structured_prompt + f"\n\nAdditional analysis requirements:\n{prompt_override}\n\nREMEMBER: Still return the JSON structure specified above."
        else:
            final_prompt = structured_prompt

        flushprint("Sending request to Gemini...")
        response = model.generate_content([final_prompt, pil_img])

        if not response.text or not response.text.strip():
            flushprint("Empty response – retrying without image")
            response = model.generate_content(final_prompt)

        response_text = response.text.strip()
        flushprint(f"Received response (length: {len(response_text)})")
        
        # Clean response
        if '```json' in response_text:
            start = response_text.find('```json') + 7
            end = response_text.rfind('```')
            if end > start:
                response_text = response_text[start:end].strip()
        elif '```' in response_text:
            start = response_text.find('```') + 3
            end = response_text.rfind('```')
            if end > start:
                response_text = response_text[start:end].strip()
        
        # Remove any leading/trailing whitespace or newlines
        response_text = response_text.strip()
        
        # Log for debugging
        if len(response_text) < 500:
            flushprint(f"Full cleaned response: {response_text}")
        else:
            flushprint(f"Response preview: {response_text[:200]}...")
            flushprint(f"Response end: ...{response_text[-200:]}")
        
        try:
            # Parse JSON
            result_dict = json.loads(response_text)
            
            # Validate it's a dictionary
            if not isinstance(result_dict, dict):
                raise ValueError(f"Expected dict, got {type(result_dict)}")
            
            # Ensure critical fields
            result_dict["Platform"] = provider_name
            result_dict["URL"] = url
            
            # Validate sections are arrays
            if not isinstance(result_dict.get("Above_Fold_Sections"), list):
                flushprint("Warning: Above_Fold_Sections is not a list, creating default")
                result_dict["Above_Fold_Sections"] = ["Hero Section", "Navigation Bar"]
            
            if not isinstance(result_dict.get("Below_Fold_Sections"), list):
                flushprint("Warning: Below_Fold_Sections is not a list, creating default")
                result_dict["Below_Fold_Sections"] = ["Footer"]
            
            # Ensure Section_Details exists
            if not isinstance(result_dict.get("Section_Details"), dict):
                result_dict["Section_Details"] = {}
            
            # Count sections for logging
            above_count = len(result_dict.get("Above_Fold_Sections", []))
            below_count = len(result_dict.get("Below_Fold_Sections", []))
            flushprint(f"Success! Found {above_count} above-fold and {below_count} below-fold sections")
            
            return result_dict
            
        except json.JSONDecodeError as e:
            flushprint(f"JSON decode error: {e}")
            flushprint(f"Failed to parse: {response_text[:200]}...")
            
            # Try alternative extraction
            try:
                extracted = _extract_json(response.text)
                extracted["Platform"] = provider_name
                extracted["URL"] = url
                return extracted
            except:
                pass
            
            # Return error structure
            return {
                "Platform": provider_name,
                "URL": url,
                "Type": "error",
                "Main_Offer": "JSON Parse Error",
                "Primary_CTA": "Error",
                "Secondary_CTA": "Error",
                "Headline": "Error",
                "Subheadline": "Error",
                "Trust_Elements": "Error",
                "Visual_Design": "Error",
                "Above_Fold_Elements": "Error",
                "Pricing_Info": "Error",
                "Target_Audience": "Error",
                "Unique_Selling_Points": "Error",
                "Lead_Generation_Type": "Error",
                "Above_Fold_Sections": ["Parse Error"],
                "Below_Fold_Sections": ["Parse Error"],
                "Section_Details": {},
                "error": f"JSON parse error: {str(e)}",
                "response_length": len(response_text)
            }

    except Exception as e:
        flushprint(f"Gemini API error: {type(e).__name__}: {e}")
        return {
            "Platform": provider_name,
            "URL": url,
            "Type": "error",
            "Main_Offer": f"API Error: {type(e).__name__}",
            "Primary_CTA": "Error",
            "Secondary_CTA": "Error",
            "Headline": "Error",
            "Subheadline": "Error",
            "Trust_Elements": "Error",
            "Visual_Design": "Error",
            "Above_Fold_Elements": "Error",
            "Pricing_Info": "Error",
            "Target_Audience": "Error",
            "Unique_Selling_Points": "Error",
            "Lead_Generation_Type": "Error",
            "Above_Fold_Sections": [],
            "Below_Fold_Sections": [],
            "Section_Details": {},
            "error": str(e)
        }


def consolidate_sections_across_providers(all_course_data):
    """
    Consolidate all unique sections found across all providers
    Returns a dictionary with section names and which providers have them
    """
    flushprint("Consolidating sections across all providers")
    flushprint(f"Processing {len(all_course_data)} providers")
    
    # Collect all unique sections
    all_above_fold_sections = set()
    all_below_fold_sections = set()
    
    for i, provider_data in enumerate(all_course_data):
        flushprint(f"Processing provider {i+1}: {provider_data.get('Platform', 'Unknown')}")
        
        if 'error' in provider_data:
            flushprint(f"  Skipping due to error: {provider_data['error']}")
            continue
            
        # Collect above fold sections
        above_fold = provider_data.get('Above_Fold_Sections', [])
        if isinstance(above_fold, list):
            # Normalize section names to avoid duplicates due to case differences
            normalized_sections = [section.strip() for section in above_fold if section and section.strip()]
            all_above_fold_sections.update(normalized_sections)
            flushprint(f"  Found {len(above_fold)} above-fold sections: {above_fold[:3]}...")  # Show first 3
        else:
            flushprint(f"  Warning: Above_Fold_Sections is not a list: {type(above_fold)}")
        
        # Collect below fold sections
        below_fold = provider_data.get('Below_Fold_Sections', [])
        if isinstance(below_fold, list):
            # Normalize section names to avoid duplicates due to case differences
            normalized_sections = [section.strip() for section in below_fold if section and section.strip()]
            all_below_fold_sections.update(normalized_sections)
            flushprint(f"  Found {len(below_fold)} below-fold sections: {below_fold[:3]}...")  # Show first 3
        else:
            flushprint(f"  Warning: Below_Fold_Sections is not a list: {type(below_fold)}")
    
    flushprint(f"Total unique above-fold sections: {len(all_above_fold_sections)}")
    flushprint(f"Total unique below-fold sections: {len(all_below_fold_sections)}")
    
    # If no sections found, add generic defaults
    if not all_above_fold_sections:
        all_above_fold_sections = {
            "Hero Section", "Navigation Bar", "Value Proposition", 
            "Call-to-Action", "Trust Indicators"
        }
        flushprint("No above-fold sections found, using defaults")
    
    if not all_below_fold_sections:
        all_below_fold_sections = {
            "Features Section", "Testimonials", "Pricing", 
            "FAQ", "Footer", "Contact Information"
        }
        flushprint("No below-fold sections found, using defaults")
    
    return {
        'above_fold': sorted(list(all_above_fold_sections)),
        'below_fold': sorted(list(all_below_fold_sections))
    }


def create_section_comparison_dataframe(all_course_data):
    """
    Create a DataFrame showing which providers have which sections
    Similar to the screenshot example with checkmarks
    """
    flushprint("Creating section comparison dataframe")
    
    # Filter out error entries for column headers
    valid_providers = [p for p in all_course_data if 'error' not in p]
    
    if not valid_providers:
        flushprint("No valid providers found!")
        return pd.DataFrame({"Section": ["No valid data collected"], "Status": ["Error"]})
    
    # Get all unique sections
    sections = consolidate_sections_across_providers(all_course_data)
    
    # Create comparison data
    comparison_data = []
    
    # Create header row
    header_row = {'Section': 'Platform'}
    for provider in valid_providers:
        header_row[provider['Platform']] = provider['Platform']
    comparison_data.append(header_row)
    
    # Add URL row
    url_row = {'Section': 'URL'}
    for provider in valid_providers:
        url_row[provider['Platform']] = provider.get('URL', 'N/A')
    comparison_data.append(url_row)
    
    # Add separator for above fold
    separator_row = {'Section': '=== ABOVE THE FOLD ==='}
    for provider in valid_providers:
        separator_row[provider['Platform']] = ''
    comparison_data.append(separator_row)
    
    # Add above fold sections
    for section in sections['above_fold']:
        row = {'Section': section}
        for provider in all_course_data:
            if 'error' in provider:
                continue
            provider_name = provider['Platform']
            if provider_name in [p['Platform'] for p in valid_providers]:
                above_fold = provider.get('Above_Fold_Sections', [])
                if isinstance(above_fold, list) and section in above_fold:
                    row[provider_name] = '✅'
                else:
                    row[provider_name] = '❌'
        comparison_data.append(row)
    
    # Add separator for below fold
    separator_row2 = {'Section': '=== BELOW THE FOLD ==='}
    for provider in valid_providers:
        separator_row2[provider['Platform']] = ''
    comparison_data.append(separator_row2)
    
    # Add below fold sections
    for section in sections['below_fold']:
        row = {'Section': section}
        for provider in all_course_data:
            if 'error' in provider:
                continue
            provider_name = provider['Platform']
            if provider_name in [p['Platform'] for p in valid_providers]:
                below_fold = provider.get('Below_Fold_Sections', [])
                if isinstance(below_fold, list) and section in below_fold:
                    row[provider_name] = '✅'
                else:
                    row[provider_name] = '❌'
        comparison_data.append(row)
    
    # Add section count summary
    summary_row = {'Section': '=== SECTION COUNTS ==='}
    for provider in valid_providers:
        summary_row[provider['Platform']] = ''
    comparison_data.append(summary_row)
    
    # Add above fold count
    count_row_above = {'Section': 'Total Above Fold Sections'}
    for provider in all_course_data:
        if 'error' in provider:
            continue
        provider_name = provider['Platform']
        if provider_name in [p['Platform'] for p in valid_providers]:
            above_fold = provider.get('Above_Fold_Sections', [])
            if isinstance(above_fold, list):
                count_row_above[provider_name] = str(len(above_fold))
            else:
                count_row_above[provider_name] = '0'
    comparison_data.append(count_row_above)
    
    # Add below fold count
    count_row_below = {'Section': 'Total Below Fold Sections'}
    for provider in all_course_data:
        if 'error' in provider:
            continue
        provider_name = provider['Platform']
        if provider_name in [p['Platform'] for p in valid_providers]:
            below_fold = provider.get('Below_Fold_Sections', [])
            if isinstance(below_fold, list):
                count_row_below[provider_name] = str(len(below_fold))
            else:
                count_row_below[provider_name] = '0'
    comparison_data.append(count_row_below)
    
    # Create DataFrame
    df = pd.DataFrame(comparison_data)
    
    flushprint(f"Section comparison table created with {len(df)} rows and {len(df.columns)} columns")
    
    return df


def create_section_details_dataframe(all_course_data):
    """
    Create a DataFrame showing the specific implementation details for each section
    """
    flushprint("Creating section details dataframe")
    
    # Filter out error entries
    valid_providers = [p for p in all_course_data if 'error' not in p]
    
    details_data = []
    
    for provider in valid_providers:
        section_details = provider.get('Section_Details', {})
        if section_details and isinstance(section_details, dict):
            for section_name, details in section_details.items():
                details_data.append({
                    'Platform': provider['Platform'],
                    'Section': section_name,
                    'Implementation': details
                })
    
    if details_data:
        details_df = pd.DataFrame(details_data)
        return details_df
    
    return None


def generate_summary_report(course_data_df: pd.DataFrame, client_name: str, section_comparison_df: pd.DataFrame = None) -> str:
    flushprint(f"Generating summary report with client: {client_name}")
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        
        data_string = course_data_df.to_csv(index=False)
        
        # Include section comparison if available
        section_comparison_string = ""
        if section_comparison_df is not None:
            section_comparison_string = f"""
        
        **Section Presence Comparison Table:**
        ```csv
        {section_comparison_df.to_csv(index=False)}
        ```"""
        
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
        {section_comparison_string}

        🎯 **Your Task:**
        1. Write a **Strategic Executive Summary** for {client_name} identifying key conversion optimization opportunities
        2. Create a **Competitive Analysis Overview** highlighting how {client_name} compares to competitors
        3. Provide a **Section Gap Analysis** based on the section comparison table:
           - Identify critical sections that competitors have but {client_name} is missing
           - Highlight unique sections that {client_name} has as competitive advantages
           - Note: Sections are dynamically identified based on actual content found on each site
           - Consider industry-specific sections that may be important for conversion
           - Recommend priority sections to add based on competitor insights and best practices
        4. Provide a **Priority Opportunities Table** with:
           - Opportunity
           - Impact Level (High/Medium/Low)
           - Implementation Difficulty (Easy/Medium/Hard)
           - Rationale (competitor insight or CRO best practice)
           - Specific Tactical Recommendations
        5. Conclude with **Strategic Recommendations** for immediate action

        **Focus Areas:**
        - Above-the-fold optimization
        - Below-the-fold content gaps and opportunities
        - CTA placement and messaging
        - Trust signal improvements
        - Value proposition clarity
        - User experience enhancements
        - Conversion funnel optimization
        - Missing sections that could improve conversion

        Present your response in clean, professional markdown format suitable for executive presentation.
        """

        response = model.generate_content(prompt)
        return response.text
        
    except Exception as e:
        flushprint(f"Failed to generate summary report: {e}")
        return f"Error generating summary report: {str(e)}"

# -- Main analyzer function --
def analyze_landing_pages(landing_pages, prompt_override=None, structured_prompt_override=None):
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
                        # Pass landing_pages as the last parameter for context
                        structured_data = get_multimodal_analysis_from_gemini(
                            page_content, image_bytes, lp['name'], lp['url'], prompt_override, structured_prompt_override, landing_pages
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
                        # Pass landing_pages as the last parameter for context
                        structured_data = get_multimodal_analysis_from_gemini(
                            page_content, image_bytes, lp['name'], lp['url'], prompt_override, structured_prompt_override, landing_pages
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

    # Save main analysis CSV (local and database)
    df = pd.DataFrame(all_course_data)
    csv_path = os.path.join(session_dir, "competitive_analysis_data.csv")
    df.to_csv(csv_path, index=False)
    flushprint(f"CSV saved locally with {len(all_course_data)} records")
    
    # Save CSV to database
    with open(csv_path, 'rb') as f:
        csv_bytes = f.read()
    save_analysis_result_to_db(session_id, 'csv', 'competitive_analysis_data.csv', 
                               content=df.to_csv(index=False), file_data=csv_bytes, client_name=client_name)
    
    # Create section comparison table
    section_comparison_df = create_section_comparison_dataframe(all_course_data)
    section_csv_path = os.path.join(session_dir, "section_comparison_table.csv")
    section_comparison_df.to_csv(section_csv_path, index=False)
    flushprint(f"Section comparison table saved to {section_csv_path}")
    
    # Create section details DataFrame
    section_details_df = create_section_details_dataframe(all_course_data)
    if section_details_df is not None:
        details_path = os.path.join(session_dir, "section_implementation_details.csv")
        section_details_df.to_csv(details_path, index=False)
        flushprint(f"Section details saved to {details_path}")
    
    # Save section comparison to database
    with open(section_csv_path, 'rb') as f:
        section_csv_bytes = f.read()
    save_analysis_result_to_db(session_id, 'section_comparison', 'section_comparison_table.csv',
                               content=section_comparison_df.to_csv(index=False), 
                               file_data=section_csv_bytes, client_name=client_name)
    
    # Store path for downloads
    app.config['LAST_SECTION_CSV_PATH'] = section_csv_path
    
    # Generate summary report
    successful_df = df[df['error'].isnull()].copy() if 'error' in df.columns else df
    
    if not successful_df.empty:
        # Pass section_comparison_df to generate_summary_report
        summary_report = generate_summary_report(successful_df, client_name, section_comparison_df)
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
    structured_prompt = DEFAULT_STRUCTURED_PROMPT_TEMPLATE
    analysis_data = None
    section_data = None
    section_details = None
    analysis_columns = None
    section_columns = None
    client_name = None
    total_sections = 0

    if request.method == "POST":
        try:
            urls = request.form.getlist("urls[]")
            types = request.form.getlist("types[]")
            prompt = request.form.get("prompt", "")
            structured_prompt = request.form.get("structured_prompt", "")
            
            # Save manual screenshots
            uploaded_files = request.files
            uploaded_names = save_manual_screenshots(uploaded_files)
            flushprint(f"Manual screenshots saved: {uploaded_names}")
            
            # Validate inputs
            if not urls or not any(url.strip() for url in urls):
                error = "Please provide at least one URL"
                return render_template_string(HTML, error=error, prompt=prompt, default_prompt=DEFAULT_ANALYSIS_PROMPT, structured_prompt=structured_prompt)
            
            if len(urls) != len(types):
                error = "Each URL must have a corresponding type selected"
                return render_template_string(HTML, error=error, prompt=prompt, default_prompt=DEFAULT_ANALYSIS_PROMPT, structured_prompt=structured_prompt)
            
            # Check for at least one client
            if 'client' not in types:
                error = "Please designate at least one URL as 'Client'"
                return render_template_string(HTML, error=error, prompt=prompt, default_prompt=DEFAULT_ANALYSIS_PROMPT, structured_prompt=structured_prompt)
            
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
                return render_template_string(HTML, error=error, prompt=prompt, default_prompt=DEFAULT_ANALYSIS_PROMPT, structured_prompt=structured_prompt)
            
            # Run analysis with both prompts
            summary, csv_path = analyze_landing_pages(
                landing_pages, 
                prompt if prompt.strip() else None,
                structured_prompt if structured_prompt.strip() else None
            )
            success = f"Analysis completed successfully! Processed {len(landing_pages)} landing pages."
            
            # Load the analysis data for display
            session_id = app.config.get('LAST_SESSION_ID')
            if session_id:
                # Get main analysis data
                csv_result = get_analysis_result_from_db(session_id, 'csv')
                if csv_result and csv_result['content']:
                    import io
                    df = pd.read_csv(io.StringIO(csv_result['content']))
                    
                    # Convert DataFrame to list of dicts for template
                    analysis_data = df.to_dict('records')
                    analysis_columns = [col for col in df.columns if col not in ['Above_Fold_Sections', 'Below_Fold_Sections', 'Section_Details']]
                    
                    # For sections display, parse the JSON strings if they are strings
                    for row in analysis_data:
                        try:
                            if 'Above_Fold_Sections' in row and isinstance(row['Above_Fold_Sections'], str):
                                row['Above_Fold_Sections'] = json.loads(row['Above_Fold_Sections'])
                            if 'Below_Fold_Sections' in row and isinstance(row['Below_Fold_Sections'], str):
                                row['Below_Fold_Sections'] = json.loads(row['Below_Fold_Sections'])
                            if 'Section_Details' in row and isinstance(row['Section_Details'], str):
                                row['Section_Details'] = json.loads(row['Section_Details'])
                        except:
                            pass
                    
                    # Extract section details for the details table
                    section_details = []
                    for row in analysis_data:
                        if 'Section_Details' in row and isinstance(row.get('Section_Details'), dict):
                            for section_name, implementation in row['Section_Details'].items():
                                section_details.append({
                                    'Platform': row['Platform'],
                                    'Section': section_name,
                                    'Implementation': implementation
                                })
                    
                    # Get client name
                    for row in analysis_data:
                        if row.get('Type') == 'client':
                            client_name = row['Platform']
                            break
                
                # Get section comparison data
                section_result = get_analysis_result_from_db(session_id, 'section_comparison')
                if section_result and section_result['content']:
                    section_df = pd.read_csv(io.StringIO(section_result['content']))
                    section_data = section_df.to_dict('records')
                    section_columns = list(section_df.columns)
                    
                    # Count total unique sections (excluding separators and summary rows)
                    total_sections = len([row for row in section_data if '===' not in str(row.get('Section', '')) and 'Total' not in str(row.get('Section', ''))])
            
        except Exception as e:
            error = f"Analysis failed: {str(e)}"
            flushprint("Error in analysis:", e)

    return render_template_string(
        HTML, 
        summary=summary, 
        error=error, 
        success=success, 
        prompt=prompt, 
        default_prompt=DEFAULT_ANALYSIS_PROMPT,
        structured_prompt=structured_prompt,
        analysis_data=analysis_data,
        section_data=section_data,
        section_details=section_details,
        analysis_columns=analysis_columns,
        section_columns=section_columns,
        client_name=client_name,
        total_sections=total_sections
    )

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

@app.route('/download/sections')
def download_sections():
    flushprint("Download section comparison requested")
    
    # Try to get from database first
    session_id = app.config.get('LAST_SESSION_ID')
    if session_id:
        result = get_analysis_result_from_db(session_id, 'section_comparison')
        if result:
            return send_file(
                io.BytesIO(result['file_data']), 
                as_attachment=True, 
                download_name=result['file_name'],
                mimetype='text/csv'
            )
    
    # Fallback to local file
    path = app.config.get('LAST_SECTION_CSV_PATH')
    if not path or not os.path.exists(path):
        return "No section comparison file available. Please run an analysis first.", 404
    return send_file(path, as_attachment=True, download_name="section_comparison_table.csv")

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

@app.route('/create-landing-page-tables')
def create_landing_page_tables():
    """Create separate tables for landing page analysis"""
    try:
        conn = get_db_conn()
        migration_log = []
        
        try:
            with conn:
                with conn.cursor() as cur:
                    # Create a new table specifically for landing page analysis
                    migration_log.append("Creating landing_page_analysis table...")
                    
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS landing_page_analysis (
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
                    
                    migration_log.append("✅ Created landing_page_analysis table")
                    
                    # Check if it was created successfully
                    cur.execute("""
                        SELECT column_name, data_type 
                        FROM information_schema.columns 
                        WHERE table_name = 'landing_page_analysis'
                        ORDER BY ordinal_position;
                    """)
                    columns = cur.fetchall()
                    
                    migration_log.append("\nTable structure:")
                    for col in columns:
                        migration_log.append(f"  - {col['column_name']}: {col['data_type']}")
                    
                    return jsonify({
                        "status": "success",
                        "message": "Landing page tables created successfully",
                        "log": migration_log
                    })
                    
        finally:
            conn.close()
            
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"Failed to create tables: {str(e)}",
            "log": migration_log if 'migration_log' in locals() else []
        }), 500

@app.route('/migrate-db')
def migrate_database():
    """Migrate database to add missing columns"""
    try:
        conn = get_db_conn()
        migration_log = []
        
        try:
            with conn:
                with conn.cursor() as cur:
                    # First create the landing_page_analysis table if it doesn't exist
                    migration_log.append("Ensuring landing_page_analysis table exists...")
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS landing_page_analysis (
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
                    migration_log.append("✅ Landing page analysis table ready")
                    
                    # Check if old analysis_results table exists
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT FROM information_schema.tables 
                            WHERE table_name = 'analysis_results'
                        )
                    """)
                    old_table_exists = cur.fetchone()['exists']
                    
                    if old_table_exists:
                        migration_log.append("\nNote: Old analysis_results table exists")
                        migration_log.append("The app will use the new landing_page_analysis table")
                    
                    return jsonify({
                        "status": "success",
                        "message": "Database migration completed",
                        "log": migration_log
                    })
                    
        finally:
            conn.close()
            
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"Migration failed: {str(e)}",
            "log": migration_log if 'migration_log' in locals() else []
        }), 500

@app.route('/debug/last-analysis')
def debug_last_analysis():
    """Debug route to see the structure of the last analysis"""
    try:
        session_id = app.config.get('LAST_SESSION_ID')
        if not session_id:
            return jsonify({"error": "No analysis has been run yet"}), 404
        
        # Get the CSV data
        csv_result = get_analysis_result_from_db(session_id, 'csv')
        if not csv_result:
            return jsonify({"error": "No CSV data found"}), 404
        
        # Parse the CSV content
        import io
        import pandas as pd
        df = pd.read_csv(io.StringIO(csv_result['content']))
        
        # Get section comparison
        section_result = get_analysis_result_from_db(session_id, 'section_comparison')
        section_df = None
        if section_result:
            section_df = pd.read_csv(io.StringIO(section_result['content']))
        
        # Analyze the data structure
        debug_info = {
            "session_id": session_id,
            "total_providers": len(df),
            "columns_found": list(df.columns),
            "providers": list(df['Platform'].values) if 'Platform' in df.columns else [],
        }
        
        # Check each provider's data
        provider_details = []
        for idx, row in df.iterrows():
            provider_info = {
                "platform": row.get('Platform', 'Unknown'),
                "url": row.get('URL', 'Unknown'),
                "has_above_fold_sections": False,
                "has_below_fold_sections": False,
                "above_fold_sections": {},
                "below_fold_sections": {},
                "all_columns": {}
            }
            
            # Check for section columns
            if 'Above_Fold_Sections' in row:
                try:
                    sections = json.loads(row['Above_Fold_Sections']) if isinstance(row['Above_Fold_Sections'], str) else row['Above_Fold_Sections']
                    if isinstance(sections, list):
                        provider_info['has_above_fold_sections'] = True
                        provider_info['above_fold_sections'] = sections
                except:
                    provider_info['above_fold_sections'] = str(row['Above_Fold_Sections'])
            
            if 'Below_Fold_Sections' in row:
                try:
                    sections = json.loads(row['Below_Fold_Sections']) if isinstance(row['Below_Fold_Sections'], str) else row['Below_Fold_Sections']
                    if isinstance(sections, list):
                        provider_info['has_below_fold_sections'] = True
                        provider_info['below_fold_sections'] = sections
                except:
                    provider_info['below_fold_sections'] = str(row['Below_Fold_Sections'])
            
            if 'Section_Details' in row:
                try:
                    details = json.loads(row['Section_Details']) if isinstance(row['Section_Details'], str) else row['Section_Details']
                    if isinstance(details, dict):
                        provider_info['section_details'] = details
                except:
                    provider_info['section_details'] = str(row['Section_Details'])
            
            # Add all column values for debugging
            for col in df.columns:
                provider_info['all_columns'][col] = str(row[col])[:100]  # Limit to 100 chars
            
            provider_details.append(provider_info)
        
        debug_info['provider_details'] = provider_details
        
        # Add section comparison info
        if section_df is not None:
            debug_info['section_comparison'] = {
                "rows": len(section_df),
                "columns": list(section_df.columns),
                "sections_found": list(section_df['Section'].values) if 'Section' in section_df.columns else []
            }
        else:
            debug_info['section_comparison'] = "No section comparison found"
        
        return jsonify(debug_info)
        
    except Exception as e:
        return jsonify({"error": f"Debug failed: {str(e)}"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    flushprint(f"Starting Dynamic Landing Page Analyzer on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)

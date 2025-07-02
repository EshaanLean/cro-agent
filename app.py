import os
import json
import csv
import google.generativeai as genai  # Updated import
from flask import Flask, request, render_template_string, send_file
import requests
from PIL import Image
import io
import base64

app = Flask(__name__)

UPLOAD_FOLDER = "manual_screenshots"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
CSV_FILE = "competitive_analysis_results.csv"

def flushprint(*args, **kwargs):
    print(*args, **kwargs, flush=True)

def url_to_key(url):
    """Convert URL to a safe filename key"""
    return (url.replace("https://", "www_")
            .replace("http://", "www_")
            .replace(".", "_")
            .replace("/", "_")
            .replace("?", "_")
            .replace("=", "_")
            .replace(":", "_")
            .replace("-", "_"))

def take_screenshot(url):
    """
    Take a screenshot of the URL using a headless browser service
    This is a placeholder - you'll need to implement this based on your preferred method
    """
    # Option 1: Use Selenium with Chrome headless
    # Option 2: Use Playwright
    # Option 3: Use a screenshot service API
    
    # For now, return None to indicate manual screenshot is needed
    flushprint(f"Auto screenshot not implemented for {url}")
    return None

def load_image_for_gemini(image_path):
    """Load and prepare image for Gemini API"""
    try:
        with Image.open(image_path) as img:
            # Convert to RGB if needed
            if img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Resize if too large (Gemini has size limits)
            max_size = 1024
            if img.width > max_size or img.height > max_size:
                img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            
            return img
    except Exception as e:
        flushprint(f"Error loading image {image_path}: {e}")
        return None

def analyze_with_gemini(image_path, url, provider_name, prompt_template):
    """Analyze landing page screenshot with Gemini"""
    try:
        # Load image
        image = load_image_for_gemini(image_path)
        if not image:
            return None
        
        # Prepare prompt
        prompt = prompt_template.format(
            provider_name=provider_name,
            url=url,
            prompt_text_section="**Note:** Analysis based on screenshot only."
        )
        
        # Initialize Gemini model
        model = genai.GenerativeModel('gemini-2.0-flash-exp')
        
        # Generate content with image
        response = model.generate_content([prompt, image])
        
        # Parse JSON response
        response_text = response.text.strip()
        
        # Clean up response if it has markdown formatting
        if response_text.startswith('```json'):
            response_text = response_text[7:]
        if response_text.endswith('```'):
            response_text = response_text[:-3]
        
        # Parse JSON
        try:
            json_result = json.loads(response_text)
            return json_result
        except json.JSONDecodeError as e:
            flushprint(f"JSON parsing error for {url}: {e}")
            flushprint(f"Response was: {response_text[:500]}...")
            return None
            
    except Exception as e:
        flushprint(f"Error analyzing {url} with Gemini: {e}")
        return None

def create_fallback_result(url, provider_name):
    """Create a fallback result when analysis fails"""
    return {
        "Platform": provider_name,
        "LP Link": url,
        "Main Offer": "Analysis Failed",
        "Purchase or Lead Gen Form": "N/A",
        "Primary CTA": "N/A",
        "Above the Fold - Headline": "N/A",
        "Above the Fold - Trust Elements": "N/A",
        "Above the Fold - Other Elements": "N/A",
        "Above the Fold - Creative (Yes/No)": "N/A",
        "Above the Fold - Creative Type": "N/A",
        "Above the Fold - Creative Position": "N/A",
        "Above the Fold - # of CTAs": "N/A",
        "Above the Fold - CTA / Form Position": "N/A",
        "Primary CTA Just for Free Trial": "N/A",
        "Secondary CTA": "N/A",
        "Clickable Logo": "N/A",
        "Navigation Bar": "N/A"
    }

default_prompt = """
As a digital marketing and CRO (Conversion Rate Optimization) expert, analyze the provided landing page screenshot for the company '{provider_name}'.
Your goal is to populate a structured JSON object based on the visual evidence.

**Webpage Information:**
- **Provider:** {provider_name}
- **URL:** {url}
{prompt_text_section}

**Instructions:**
Carefully examine the **screenshot** for visual layout, design elements, and "above the fold" content.
Fill out the following JSON object based on what you can see in the image.

If you cannot determine a value from the screenshot, use "Not Found" or "N/A".

**JSON Structure to Populate:**
{{
  "Platform": "{provider_name}",
  "LP Link": "{url}",
  "Main Offer": "Describe the main value proposition or product offering visible in the screenshot.",
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

strategy_prompt_template = """
You are a CRO and digital strategy agent. Your goal is to analyze {client_name}'s landing page for its {theme} landing page and identify strategic opportunities to improve conversion performance.

📌 **Client Landing Page:** {client_lp}

**Compare this page against these competitor landing pages:**
{competitor_urls}

You also have access to a detailed comparison table of all pages (in CSV format below) showing:
- Above-the-fold breakdowns (headline, CTA placement, navigation, creative)
- I have also provided full-page screenshots of all pages during a previous step, which you should use for visual reference.

**Detailed Comparison Data (CSV):**
```csv
{data_string}
```

🎯 **Your task:**

Write a strategic summary for {client_name} — identifying how it can improve conversions through CRO and personalization, without changing its core offer (real-world projects, mentorship, career prep).

Create an Opportunity Table with these columns:
- **Opportunity**
- **Why It Matters**
- **Tactical Ideas**
- **Rationale / Inspiration** (note if it's competitor-based or CRO best practice)

Summarize the strategic advantage these changes would unlock.

Present your response in a clean, readable format with clear sections and actionable insights.
"""

HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>CRO Landing Page Analyzer</title>
    <style>
        body { font-family: Arial, sans-serif; margin:40px; line-height: 1.5;}
        input[type="text"] { font-size: 14px; padding: 5px; }
        textarea { width: 98%; height: 350px; font-family: monospace; font-size: 13px; padding: 10px;}
        th, td { padding:8px 6px; border: 1px solid #ddd; }
        table { border-collapse: collapse; width: 100%; }
        .tip { background:#f3f6fa; padding:14px 16px; margin:18px 0 22px 0; border-radius:7px; font-size:15px;}
        .client-highlight { background:#ffe7a2; }
        .error { background:#ffebee; color:#c62828; padding:14px 16px; margin:18px 0; border-radius:7px; }
        .success { background:#e8f5e8; color:#2e7d32; padding:14px 16px; margin:18px 0; border-radius:7px; }
        button { padding: 10px 15px; margin: 5px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; }
        button:hover { background: #0056b3; }
        .form-section { margin: 20px 0; }
    </style>
</head>
<body>
    <h1>🔍 CRO Landing Page Analyzer</h1>
    
    <div class="tip">
        <h3>📸 How to Take Screenshots (Manual Mode):</h3>
        <ul>
            <li><strong>Chrome Developer Tools:</strong> Right-click → "Inspect" (or Ctrl+Shift+I), then Ctrl+Shift+P (Cmd+Shift+P on Mac), type "screenshot", select "Capture full size screenshot".</li>
            <li><strong>Web Capture:</strong> Chrome/Edge: Three dots → More tools → Web capture.</li>
        </ul>
        <h3>📝 Screenshot Naming Convention:</h3>
        Use format: <code><strong>[key]_manual.png</strong></code><br>
        Example for <code>https://www.udemy.com/course/data-analyst/</code>:<br>
        <code><strong>www_udemy_com_course_data_analyst_manual.png</strong></code><br>
        <br>
        <strong>⚠️ Important:</strong> Use Manual mode for sites with login requirements or anti-bot protection (Cloudflare, etc.)
    </div>

    {% if error %}
    <div class="error">
        <strong>Error:</strong> {{ error }}
    </div>
    {% endif %}

    {% if summary %}
    <div class="success">
        <strong>✅ Analysis completed!</strong> Results saved to CSV.
    </div>
    {% endif %}

    <form method="post" enctype="multipart/form-data" class="form-section">
        <h3>🎯 Landing Pages to Analyze</h3>
        <table>
            <tr>
                <th style="width:50px">#</th>
                <th style="width:400px">URL</th>
                <th style="width:120px">Manual Screenshot?</th>
                <th style="width:200px">Upload Screenshot (.png)</th>
                <th style="width:80px">Client?</th>
            </tr>
            {% for entry in entries %}
            <tr {% if entry.client %}class="client-highlight"{% endif %}>
                <td>{{ loop.index }}</td>
                <td>
                    <input type="text" name="url_{{ loop.index0 }}" value="{{ entry.url }}" style="width:390px" placeholder="https://example.com/landing-page">
                </td>
                <td>
                    <input type="checkbox" name="manual_{{ loop.index0 }}" {% if entry.manual %}checked{% endif %}>
                </td>
                <td>
                    <input type="file" name="screenshot_{{ loop.index0 }}" accept=".png,.jpg,.jpeg">
                </td>
                <td>
                    <input type="radio" name="client_idx" value="{{ loop.index0 }}" {% if entry.client %}checked{% endif %}>
                </td>
            </tr>
            {% endfor %}
        </table>
        
        <div style="margin: 15px 0;">
            <button type="button" onclick="addRow()">➕ Add URL</button>
            <button type="submit">🚀 Analyze Landing Pages</button>
        </div>
        
        <div class="form-section">
            <h3>⚙️ Analysis Prompt (Advanced)</h3>
            <textarea name="prompt" placeholder="Enter custom analysis prompt...">{{ prompt }}</textarea>
        </div>
    </form>

    {% if csv_path %}
        <div class="form-section">
            <h3>📊 Results & Next Steps</h3>
            <p><a href="/download/csv" style="color: #007bff; text-decoration: none;">📥 Download CSV Results</a></p>
            
            <form action="/generate_summary" method="post" style="margin: 15px 0;">
                <input type="hidden" name="csv_path" value="{{ csv_path }}">
                <input type="hidden" name="client_name" value="{{ client_name }}">
                <input type="hidden" name="client_lp" value="{{ client_url }}">
                <input type="hidden" name="theme" value="Data Analytics">
                <input type="hidden" name="competitor_urls" value="{{ competitor_urls }}">
                <button type="submit">📋 Generate Strategic Summary & Recommendations</button>
            </form>
        </div>
    {% endif %}

    <script>
        function addRow() {
            const tbl = document.querySelector("table");
            const rowCount = tbl.rows.length;
            const row = tbl.insertRow(rowCount);
            row.innerHTML = `
                <td>${rowCount}</td>
                <td><input type="text" name="url_${rowCount-1}" value="" style="width:390px" placeholder="https://example.com/landing-page"></td>
                <td><input type="checkbox" name="manual_${rowCount-1}"></td>
                <td><input type="file" name="screenshot_${rowCount-1}" accept=".png,.jpg,.jpeg"></td>
                <td><input type="radio" name="client_idx" value="${rowCount-1}"></td>
            `;
        }
    </script>
</body>
</html>
'''

@app.route("/", methods=["GET", "POST"])
def index():
    flushprint("Index route called:", request.method)
    summary = None
    error = None
    csv_path = None
    entries = []
    prompt = default_prompt
    client_url = ""
    client_name = ""
    competitor_urls = ""
    
    if request.method == "POST":
        try:
            prompt = request.form.get("prompt") or default_prompt
            i = 0
            client_idx = request.form.get("client_idx")
            
            # Parse form entries
            while True:
                url = request.form.get(f"url_{i}")
                if not url:
                    break
                url = url.strip()
                if url:  # Only process non-empty URLs
                    key = url_to_key(url)
                    manual = request.form.get(f"manual_{i}") == "on"
                    is_client = str(i) == str(client_idx)
                    entries.append({
                        "url": url,
                        "key": key,
                        "manual": manual,
                        "client": is_client
                    })
                i += 1

            if not entries:
                error = "Please provide at least one URL to analyze."
            else:
                # Save uploaded manual screenshots
                for idx, entry in enumerate(entries):
                    file = request.files.get(f"screenshot_{idx}")
                    if file and file.filename:
                        filename = f"{entry['key']}_manual.png"
                        filepath = os.path.join(UPLOAD_FOLDER, filename)
                        file.save(filepath)
                        flushprint(f"Saved manual screenshot: {filepath}")

                # Analyze landing pages
                results, csv_path = analyze_landing_pages(entries, prompt)
                if results:
                    summary = "Analysis completed successfully!"
                    
                    # Identify client and competitors for summary
                    client_entry = next((e for e in entries if e["client"]), None)
                    competitor_entries = [e for e in entries if not e["client"]]
                    client_url = client_entry["url"] if client_entry else ""
                    client_name = client_entry["key"] if client_entry else ""
                    competitor_urls = "\n".join(e["url"] for e in competitor_entries)
                else:
                    error = "Analysis failed. Please check your screenshots and try again."
                    
        except Exception as e:
            error = f"An error occurred: {str(e)}"
            flushprint(f"Error in index route: {e}")
    else:
        # Default rows for new users
        entries = [
            {"url": "", "key": "", "manual": False, "client": False},
            {"url": "", "key": "", "manual": False, "client": False},
        ]

    return render_template_string(
        HTML,
        prompt=prompt,
        summary=summary,
        error=error,
        csv_path=csv_path,
        entries=entries,
        client_name=client_name,
        client_url=client_url,
        competitor_urls=competitor_urls
    )

@app.route("/download/csv")
def download_csv():
    if os.path.exists(CSV_FILE):
        return send_file(CSV_FILE, as_attachment=True)
    else:
        return "CSV file not found.", 404

@app.route("/generate_summary", methods=["POST"])
def generate_summary():
    try:
        csv_path = request.form["csv_path"]
        client_name = request.form["client_name"]
        client_lp = request.form["client_lp"]
        theme = request.form.get("theme", "Data Analytics")
        competitor_urls = request.form["competitor_urls"]
        
        # Read CSV data
        with open(csv_path, "r", encoding="utf-8") as f:
            data_string = f.read()
        
        # Generate prompt
        prompt = strategy_prompt_template.format(
            client_name=client_name,
            client_lp=client_lp,
            theme=theme,
            competitor_urls=competitor_urls,
            data_string=data_string
        )
        
        # Generate summary with Gemini
        model = genai.GenerativeModel('gemini-2.0-flash-exp')
        flushprint("Generating strategic summary via Gemini")
        response = model.generate_content(prompt)
        summary_text = response.text
        
        return render_template_string("""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Strategic Summary</title>
                <style>
                    body { font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }
                    .summary { background: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0; }
                    .back-link { display: inline-block; margin: 20px 0; padding: 10px 15px; background: #007bff; color: white; text-decoration: none; border-radius: 4px; }
                    .back-link:hover { background: #0056b3; }
                    pre { white-space: pre-wrap; word-wrap: break-word; }
                </style>
            </head>
            <body>
                <h1>📊 Strategic Summary & Recommendations</h1>
                <div class="summary">
                    <pre>{{ summary_text }}</pre>
                </div>
                <a href="/" class="back-link">← Back to Analyzer</a>
            </body>
            </html>
        """, summary_text=summary_text)
        
    except Exception as e:
        flushprint(f"Error generating summary: {e}")
        return f"Error generating summary: {str(e)}", 500

def analyze_landing_pages(entries, prompt):
    """
    Analyze landing pages using Gemini API with screenshots
    """
    flushprint("Starting landing page analysis...")
    results = []
    
    # CSV headers
    header = [
        "Platform", "LP Link", "Main Offer", "Purchase or Lead Gen Form", "Primary CTA",
        "Above the Fold - Headline", "Above the Fold - Trust Elements", "Above the Fold - Other Elements",
        "Above the Fold - Creative (Yes/No)", "Above the Fold - Creative Type", "Above the Fold - Creative Position",
        "Above the Fold - # of CTAs", "Above the Fold - CTA / Form Position", "Primary CTA Just for Free Trial",
        "Secondary CTA", "Clickable Logo", "Navigation Bar"
    ]
    
    rows = []
    
    for entry in entries:
        flushprint(f"Processing {entry['key']} ({entry['url']}) manual={entry['manual']}")
        
        # Determine screenshot path
        manual_path = os.path.join(UPLOAD_FOLDER, f"{entry['key']}_manual.png")
        auto_path = os.path.join(UPLOAD_FOLDER, f"{entry['key']}_auto.png")
        
        screenshot_path = None
        if entry['manual'] and os.path.exists(manual_path):
            screenshot_path = manual_path
        elif os.path.exists(auto_path):
            screenshot_path = auto_path
        elif not entry['manual']:
            # Try to take automatic screenshot
            auto_screenshot = take_screenshot(entry['url'])
            if auto_screenshot:
                screenshot_path = auto_path
        
        if screenshot_path and os.path.exists(screenshot_path):
            # Analyze with Gemini
            flushprint(f"Analyzing {entry['url']} with screenshot: {screenshot_path}")
            json_result = analyze_with_gemini(
                screenshot_path, 
                entry['url'], 
                entry['key'], 
                prompt
            )
            
            if not json_result:
                flushprint(f"Gemini analysis failed for {entry['url']}, using fallback")
                json_result = create_fallback_result(entry['url'], entry['key'])
                
        else:
            flushprint(f"No screenshot available for {entry['url']}, using fallback")
            json_result = create_fallback_result(entry['url'], entry['key'])
        
        # Ensure all required fields are present
        for field in header:
            if field not in json_result:
                json_result[field] = "N/A"
        
        # Add to results
        results.append(json_result)
        rows.append([json_result.get(h, "N/A") for h in header])
    
    # Save to CSV
    try:
        with open(CSV_FILE, "w", newline='', encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for row in rows:
                writer.writerow(row)
        flushprint(f"CSV saved to {CSV_FILE}")
        return results, CSV_FILE
    except Exception as e:
        flushprint(f"Error saving CSV: {e}")
        return None, None

# Configure Gemini API
def initialize_gemini():
    """Initialize Gemini API with error handling"""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        flushprint("Warning: GEMINI_API_KEY not found in environment variables")
        return False
    
    try:
        genai.configure(api_key=api_key)
        flushprint("Gemini API configured successfully")
        return True
    except Exception as e:
        flushprint(f"Error configuring Gemini API: {e}")
        return False

if __name__ == "__main__":
    # Initialize Gemini API
    if not initialize_gemini():
        flushprint("Warning: Running without Gemini API - analysis will use fallback data")
    
    # Run Flask app
    flushprint("Starting Flask app...")
    app.run(host="0.0.0.0", port=10000, debug=True)

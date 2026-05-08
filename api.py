from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import fitz  # PyMuPDF
from langchain_core.documents import Document
from transformers import CLIPProcessor, CLIPModel
from PIL import Image
import torch
import numpy as np
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
import base64
import io
import os
from dotenv import load_dotenv
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import psycopg2
import secrets
import json
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.colors import HexColor

app = FastAPI()

# CORS FOR FRONTEND
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# environment variables loading
load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
if api_key:
    os.environ["OPENAI_API_KEY"] = api_key
else:
    raise ValueError("OPENAI_API_KEY not found in environment or .env file.")

# Database Configuration
DB_URL = "postgresql://multimodal_user:WvHHj4NBSrktd5OFEUcJePu1aNIZmLFk@dpg-d4o566qdbo4c73a8ik50-a.oregon-postgres.render.com/multimodal"

# Email Configuration
EMAIL_ADDRESS = "sohampattan@gmail.com"
EMAIL_PASSWORD = "kkxr gbtc wzzr vckk"

def get_db_connection():
    return psycopg2.connect(DB_URL)

import datetime

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    # Users table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email VARCHAR(255) UNIQUE NOT NULL,
            token VARCHAR(255) NOT NULL
        );
    """)
    
    # Add new columns if they don't exist (PostgreSQL specific safe add)
    try:
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_type VARCHAR(50) DEFAULT 'free'")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS query_count INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_query_date DATE DEFAULT CURRENT_DATE")
        conn.commit()
    except Exception as e:
        print(f"Migration error: {e}")
        conn.rollback()

    # Coupons table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS coupons (
            id SERIAL PRIMARY KEY,
            code VARCHAR(50) UNIQUE NOT NULL,
            discount INTEGER NOT NULL,
            is_active BOOLEAN DEFAULT TRUE
        );
    """)
    
    # Check/Create Admin
    admin_email = "sohampattan@gmail.com"
    admin_token = "qazx"
    cur.execute("SELECT * FROM users WHERE email = %s", (admin_email,))
    if not cur.fetchone():
        cur.execute("INSERT INTO users (email, token, plan_type) VALUES (%s, %s, 'pro')", (admin_email, admin_token))
        
    conn.commit()
    cur.close()
    conn.close()

def send_email(to_email, token):
    msg = MIMEMultipart()
    msg['From'] = EMAIL_ADDRESS
    msg['To'] = to_email
    msg['Subject'] = "Welcome to InsightPDF - Your Access Token"

    body = f"Welcome to InsightPDF!\n\nYour access token is: {token}\n\nPlease use this token to login."
    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        text = msg.as_string()
        server.sendmail(EMAIL_ADDRESS, to_email, text)
        server.quit()
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False

@app.on_event("startup")
def startup_event():
    init_db()

clip_model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
clip_processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
clip_model.eval()
llm = init_chat_model("gpt-4o-mini")

#memory storage
pdf_sessions = {}

# Embedding functions
def embed_image(image_data):
    if isinstance(image_data, str):
        image = Image.open(image_data).convert("RGB")
    else:
        image = image_data
    inputs = clip_processor(images=image, return_tensors="pt")
    with torch.no_grad():
        features = clip_model.get_image_features(**inputs)
        features = features / features.norm(dim=-1, keepdim=True)
        return features.squeeze().numpy()

def embed_text(text):
    inputs = clip_processor(
        text=text,
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=77
    )
    with torch.no_grad():
        features = clip_model.get_text_features(**inputs)
        features = features / features.norm(dim=-1, keepdim=True)
        return features.squeeze().numpy()

@app.post("/register")
async def register(email: str = Form(...)):
    token = secrets.token_hex(16)
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Check if user exists
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cur.fetchone()
        if user:
            # Update token
            cur.execute("UPDATE users SET token = %s WHERE email = %s", (token, email))
        else:
            # Create user
            cur.execute("INSERT INTO users (email, token) VALUES (%s, %s)", (email, token))
        conn.commit()
        
        if send_email(email, token):
            return JSONResponse(content={"message": "Registration successful. Check your email."}, status_code=200)
        else:
            return JSONResponse(content={"error": "Failed to send email."}, status_code=500)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        cur.close()
        conn.close()

@app.post("/login")
async def login(email: str = Form(...), token: str = Form(...)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT email, plan_type FROM users WHERE email = %s AND token = %s", (email, token))
        user = cur.fetchone()
        if user:
            return JSONResponse(content={
                "message": "Login successful",
                "email": user[0],
                "plan_type": user[1] or "free"
            }, status_code=200)
        else:
            return JSONResponse(content={"error": "Invalid credentials"}, status_code=401)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        cur.close()
        conn.close()

@app.post("/update_token")
async def update_token(email: str = Form(...), current_token: str = Form(...), new_token: str = Form(...)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Verify current token
        cur.execute("SELECT * FROM users WHERE email = %s AND token = %s", (email, current_token))
        user = cur.fetchone()
        
        if not user:
            return JSONResponse(content={"error": "Invalid current password"}, status_code=401)

        # Update to new token
        cur.execute("UPDATE users SET token = %s WHERE email = %s", (new_token, email))
        conn.commit()
        
        if cur.rowcount > 0:
            return JSONResponse(content={"message": "Password updated successfully"}, status_code=200)
        else:
            return JSONResponse(content={"error": "User not found"}, status_code=404)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        cur.close()
        conn.close()

@app.get("/admin/coupons")
async def get_coupons():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT code, discount, is_active FROM coupons")
        coupons = [{"code": row[0], "discount": row[1], "is_active": row[2]} for row in cur.fetchall()]
        return JSONResponse(content={"coupons": coupons}, status_code=200)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        cur.close()
        conn.close()

@app.post("/admin/add_coupon")
async def add_coupon(code: str = Form(...), discount: int = Form(...)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO coupons (code, discount) VALUES (%s, %s)", (code, discount))
        conn.commit()
        return JSONResponse(content={"message": "Coupon added successfully"}, status_code=200)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        cur.close()
        conn.close()

@app.post("/admin/delete_coupon")
async def delete_coupon(code: str = Form(...)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM coupons WHERE code = %s", (code,))
        conn.commit()
        return JSONResponse(content={"message": "Coupon deleted successfully"}, status_code=200)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        cur.close()
        conn.close()

@app.post("/forgot_password")
async def forgot_password(email: str = Form(...)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Check if user exists
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cur.fetchone()
        
        if not user:
            return JSONResponse(content={"error": "User not found"}, status_code=404)

        # Generate new token
        new_token = secrets.token_hex(16)
        
        # Update token
        cur.execute("UPDATE users SET token = %s WHERE email = %s", (new_token, email))
        conn.commit()
        
        if send_email(email, new_token):
            return JSONResponse(content={"message": "New access token sent to your email."}, status_code=200)
        else:
            return JSONResponse(content={"error": "Failed to send email."}, status_code=500)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        cur.close()
        conn.close()

@app.post("/upgrade_plan")
async def upgrade_plan(email: str = Form(...), coupon_code: str = Form(...)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Verify coupon
        cur.execute("SELECT * FROM coupons WHERE code = %s AND is_active = TRUE", (coupon_code,))
        coupon = cur.fetchone()
        if not coupon:
            return JSONResponse(content={"error": "Invalid or inactive coupon code"}, status_code=400)
        
        # Update user plan
        cur.execute("UPDATE users SET plan_type = 'pro' WHERE email = %s", (email,))
        conn.commit()
        
        if cur.rowcount > 0:
            return JSONResponse(content={"message": "Plan upgraded to Pro successfully"}, status_code=200)
        else:
            return JSONResponse(content={"error": "User not found"}, status_code=404)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        cur.close()
        conn.close()

@app.post("/upload_pdf")
async def upload_pdf(file: UploadFile = File(...), session_id: str = Form(...)):
    contents = await file.read()
    doc = fitz.open(stream=contents, filetype="pdf")
    all_docs = []#stores text chunks and image chunks
    all_embeddings = [] #stores CLIP and text embeeding
    image_data_store = {} #stores base64 images
    splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=100) #breaking long text into small chunks
    for i, page in enumerate(doc):
        text = page.get_text()
        if text.strip():
            temp_doc = Document(page_content=text, metadata={"page": i, "type": "text"})
            text_chunks = splitter.split_documents([temp_doc])
            for chunk in text_chunks:
                embedding = embed_text(chunk.page_content)
                all_embeddings.append(embedding)
                all_docs.append(chunk)
        for img_index, img in enumerate(page.get_images(full=True)):
            try:
                xref = img[0]
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]
                pil_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                image_id = f"page_{i}_img_{img_index}"
                buffered = io.BytesIO()
                pil_image.save(buffered, format="PNG")
                img_base64 = base64.b64encode(buffered.getvalue()).decode()
                image_data_store[image_id] = img_base64
                embedding = embed_image(pil_image)
                all_embeddings.append(embedding)
                image_doc = Document(
                    page_content=f"[Image: {image_id}]",
                    metadata={"page": i, "type": "image", "image_id": image_id}
                )
                all_docs.append(image_doc)
            except Exception as e:
                continue
    doc.close()
    embeddings_array = np.array(all_embeddings)
    vector_store = FAISS.from_embeddings(
        text_embeddings=[(doc.page_content, emb) for doc, emb in zip(all_docs, embeddings_array)],
        embedding=None,
        metadatas=[doc.metadata for doc in all_docs],
    )
    pdf_sessions[session_id] = {
        "vector_store": vector_store,
        "image_data_store": image_data_store,
        "all_docs": all_docs,
        "pdf_bytes": contents  # Store original PDF bytes
    }
    return JSONResponse({"status": "success"})

@app.post("/get_user_plan")
async def get_user_plan(email: str = Form(...)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT plan_type FROM users WHERE email = %s", (email,))
        result = cur.fetchone()
        if result:
            return JSONResponse(content={"plan": result[0]}, status_code=200)
        else:
            return JSONResponse(content={"error": "User not found"}, status_code=404)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        cur.close()
        conn.close()

@app.post("/ask")
async def ask_question(session_id: str = Form(...), question: str = Form(...), email: str = Form(None)):
    # Check Plan Limits
    if email:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT plan_type, query_count, last_query_date FROM users WHERE email = %s", (email,))
            user_data = cur.fetchone()
            if user_data:
                plan_type, query_count, last_query_date = user_data
                today = datetime.date.today()
                
                if plan_type == 'free':
                    if last_query_date != today:
                        # Reset count for new day
                        cur.execute("UPDATE users SET query_count = 1, last_query_date = %s WHERE email = %s", (today, email))
                        conn.commit()
                    elif query_count >= 5:
                        return JSONResponse(content={"error": "Daily limit reached. Please upgrade to Pro."}, status_code=403)
                    else:
                        cur.execute("UPDATE users SET query_count = query_count + 1 WHERE email = %s", (email,))
                        conn.commit()
        except Exception as e:
            print(f"Limit check error: {e}")
        finally:
            cur.close()
            conn.close()

    session = pdf_sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    vector_store = session["vector_store"]
    image_data_store = session["image_data_store"]
    all_docs = session["all_docs"]
    query_embedding = embed_text(question)
    results = vector_store.similarity_search_by_vector(
        embedding=query_embedding,
        k=5
    )
    # Build multimodal message
    content = []
    content.append({
        "type": "text",
        "text": f"Question: {question}\n\nContext:\n"
    })
    text_docs = [doc for doc in results if doc.metadata.get("type") == "text"]
    image_docs = [doc for doc in results if doc.metadata.get("type") == "image"]
    if text_docs:
        text_context = "\n\n".join([
            f"[Page {doc.metadata['page']}]: {doc.page_content}" for doc in text_docs
        ])
        content.append({
            "type": "text",
            "text": f"Text excerpts:\n{text_context}\n"
        })
    for doc in image_docs:
        image_id = doc.metadata.get("image_id")
        if image_id and image_id in image_data_store:
            content.append({
                "type": "text",
                "text": f"\n[Image from page {doc.metadata['page']}]:\n"
            })
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{image_data_store[image_id]}"
                }
            })
    content.append({
        "type": "text",
        "text": "\n\nPlease answer the question based on the provided text and images."
    })
    message = HumanMessage(content=content)
    response = llm.invoke([message])
    return JSONResponse({"answer": response.content})

@app.get("/")
def root():
    return {"status": "API running"}

@app.post("/export_chat_pdf")
async def export_chat_pdf(session_id: str = Form(...), messages: str = Form(...), pdf_base64: str = Form(None)):
    """Export original PDF pages followed by Q&A in a single PDF"""
    try:
        # Parse messages
        chat_messages = json.loads(messages)
        
        # Get original PDF from session
        session = pdf_sessions.get(session_id)
        pdf_bytes = None
        if session and "pdf_bytes" in session:
            pdf_bytes = session["pdf_bytes"]
        
        # Create output PDF
        output_pdf = fitz.open()
        
        # Add original PDF pages if available
        if pdf_bytes:
            original_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            output_pdf.insert_pdf(original_doc)
            original_doc.close()
            
            # Add a page break and Q&A section header
            output_pdf.new_page(pno=-1, width=612, height=792)  # Letter size
        
        # Now add Q&A content using reportlab
        qa_buffer = io.BytesIO()
        doc = SimpleDocTemplate(qa_buffer, pagesize=letter,
                                rightMargin=0.5*inch, leftMargin=0.5*inch,
                                topMargin=0.5*inch, bottomMargin=0.5*inch)
        
        story = []
        styles = getSampleStyleSheet()
        
        # Custom styles
        section_style = ParagraphStyle(
            'SectionTitle',
            parent=styles['Heading2'],
            fontSize=14,
            textColor=HexColor('#1e1f1f'),
            spaceAfter=12,
            spaceBefore=12,
            fontName='Helvetica-Bold',
            borderColor=HexColor('#3b82f6'),
            borderWidth=2,
            borderPadding=10
        )
        
        question_label_style = ParagraphStyle(
            'QuestionLabel',
            parent=styles['Normal'],
            fontSize=10,
            textColor=HexColor('#fff'),
            fontName='Helvetica-Bold',
            backgroundColor=HexColor('#3b82f6'),
            borderPadding=6
        )
        
        answer_label_style = ParagraphStyle(
            'AnswerLabel',
            parent=styles['Normal'],
            fontSize=10,
            textColor=HexColor('#fff'),
            fontName='Helvetica-Bold',
            backgroundColor=HexColor('#10b981'),
            borderPadding=6
        )
        
        content_style = ParagraphStyle(
            'Content',
            parent=styles['Normal'],
            fontSize=9,
            spaceAfter=10,
            leftIndent=0,
            leading=12
        )
        
        # Add Q&A section header
        story.append(Paragraph("💬 Questions & Answers", section_style))
        story.append(Spacer(1, 0.15*inch))
        
        # Add chat messages
        for idx, msg in enumerate(chat_messages, 1):
            if msg['sender'] == 'user':
                # Question label
                story.append(Paragraph('<b>Q:</b>', question_label_style))
                story.append(Spacer(1, 0.08*inch))
                # Question text
                story.append(Paragraph(msg['text'], content_style))
            else:
                # Answer label
                story.append(Paragraph('<b>A:</b>', answer_label_style))
                story.append(Spacer(1, 0.08*inch))
                # Answer text
                story.append(Paragraph(msg['text'], content_style))
            
            story.append(Spacer(1, 0.2*inch))
        
        # Build reportlab PDF
        doc.build(story)
        qa_buffer.seek(0)
        
        # Merge reportlab PDF with original PDF
        qa_doc = fitz.open(stream=qa_buffer.read(), filetype="pdf")
        output_pdf.insert_pdf(qa_doc)
        qa_doc.close()
        
        # Save to bytes
        output_buffer = io.BytesIO()
        output_pdf.save(output_buffer)
        output_pdf.close()
        output_buffer.seek(0)
        
        # Return as streaming response
        return StreamingResponse(
            iter([output_buffer.getvalue()]),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=chat_export_{session_id}.pdf"}
        )
    
    except Exception as e:
        print(f"Export error: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)

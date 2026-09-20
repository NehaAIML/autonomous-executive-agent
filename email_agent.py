import os
import sys
import json
import base64
import sqlite3
from datetime import datetime, timedelta
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel, Field

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send'
]

class EmailSummary(BaseModel):
    subject: str = "No Subject"
    sender: str = "Unknown"
    one_line_summary: str = "No summary available."
    action_items: list[str] = Field(default_factory=list)
    urgency: str = "Medium"

def get_gmail_service():
    creds = None
    token_path = 'token.json' if os.path.exists('token.json') else 'gmail-agent/token.json'
    cred_path = 'credentials.json' if os.path.exists('credentials.json') else 'gmail-agent/credentials.json'

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        from google.auth.transport.requests import Request
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(cred_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

def init_db():
    db_path = 'agent_vault.db' if not os.path.exists('gmail-agent') else 'gmail-agent/agent_vault.db'
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS processed_emails (
            message_id TEXT PRIMARY KEY,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    return conn

def is_processed(conn, message_id):
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM processed_emails WHERE message_id = ?', (message_id,))
    return cursor.fetchone() is not None

def mark_processed(conn, message_id):
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO processed_emails (message_id) VALUES (?)', (message_id,))
    conn.commit()

def analyze_with_llm(subject, sender, body):
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()

    prompt = f"""You are an executive assistant. Evaluate this email and output a strictly valid JSON response.

Evaluation Criteria for Urgency:
- High: Requires immediate personal action, time-sensitive security alert, critical business/financial deadline, or urgent direct human request.
- Medium: Useful updates, newsletters of direct professional interest, scheduled upcoming calendar events, or non-critical account notifications.
- Low: Marketing promotions, retail discount coupons, social media digests, automated notifications, or general bulk spam.

Format your response strictly as this JSON structure:
{{
  "subject": "{subject}",
  "sender": "{sender}",
  "one_line_summary": "<A 1-sentence synthesis of what this email is actually informing the reader>",
  "action_items": ["<concise action item if any, otherwise leave empty>"],
  "urgency": "<High, Medium, or Low>"
}}

Email to analyze:
Subject: {subject}
Sender: {sender}
Body Preview:
{body[:2000]}
"""

    if groq_key:
        try:
            from groq import Groq
            client = Groq(api_key=groq_key)
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": "You are a concise executive assistant. Always output valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"}
            )
            raw = response.choices[0].message.content.strip()
            data = json.loads(raw)
            return EmailSummary(
                subject=data.get("subject", subject),
                sender=data.get("sender", sender),
                one_line_summary=data.get("one_line_summary", "Summary not provided."),
                action_items=data.get("action_items", []),
                urgency=data.get("urgency", "Medium")
            )
        except Exception as e:
            print(f"[!] Groq API Call Failed: {repr(e)}")
            urgency = "Low" if any(w in (sender + subject).lower() for w in ["deal", "sale", "travelzoo", "zulily", "facebook", "pinterest"]) else "Medium"
            return EmailSummary(subject=subject, sender=sender, one_line_summary=f"Update from {sender}", action_items=[], urgency=urgency)

    try:
        import ollama
        client = ollama.Client(timeout=120)
        response = client.chat(
            model="llama3.1:8b",
            messages=[
                {"role": "system", "content": "You are a concise executive assistant. Always output valid JSON."},
                {"role": "user", "content": prompt}
            ],
            format="json"
        )
        raw = response['message']['content'].strip()
        data = json.loads(raw)
        return EmailSummary.model_validate(data)
    except Exception as e:
        print(f"[!] Ollama API Call Failed: {repr(e)}")
        return EmailSummary(subject=subject, sender=sender, one_line_summary="Local inference unavailable.", action_items=[], urgency="Medium")

def send_digest_email(service, summaries):
    today_str = datetime.now().strftime('%A, %B %d, %Y')
    body_lines = [
        f"Good morning Neha,\n\nHere is your daily executive email digest for {today_str}.\n",
        f"Total Ingested Messages: {len(summaries)}\n",
        "=" * 60 + "\n"
    ]

    priority_order = {"HIGH": 1, "MEDIUM": 2, "LOW": 3}
    sorted_summaries = sorted(summaries, key=lambda s: priority_order.get(s.urgency.upper(), 4))

    for s in sorted_summaries:
        body_lines.append(f"[{s.urgency.upper()}] {s.subject}")
        body_lines.append(f"From: {s.sender}")
        body_lines.append(f"Summary: {s.one_line_summary}")
        if s.action_items:
            body_lines.append("Action Items:")
            for item in s.action_items:
                body_lines.append(f"  • {item}")
        body_lines.append("-" * 40 + "\n")

    body_lines.append("Have a productive day!\n— Automated Executive Assistant")
    email_text = "\n".join(body_lines)

    msg = MIMEText(email_text)
    msg['to'] = 'neha.purohit.ai@gmail.com'
    msg['subject'] = f"Daily Executive Email Digest - {today_str}"

    raw_msg = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId='me', body={'raw': raw_msg}).execute()
    print("[✓] Executive digest sent successfully to neha.purohit.ai@gmail.com")

def run_daily_agent():
    service = get_gmail_service()
    conn = init_db()

    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y/%m/%d')
    query = f'in:inbox -subject:"Daily Executive Email Digest" after:{yesterday}'

    print(f"[*] Fetching emails with query: '{query}'...")
    results = service.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])

    if not messages:
        print("[*] No matching emails found to process.")
        return

    summaries = []
    for m in messages:
        m_id = m['id']
        if is_processed(conn, m_id):
            continue

        details = service.users().messages().get(userId='me', id=m_id, format='full').execute()
        headers = {h['name']: h['value'] for h in details.get('payload', {}).get('headers', [])}
        subject = headers.get('Subject', 'No Subject')
        sender = headers.get('From', 'Unknown')

        snippet = details.get('snippet', '')
        print(f"[*] Processing: {subject[:50]}...")

        summary = analyze_with_llm(subject, sender, snippet)
        summaries.append(summary)
        mark_processed(conn, m_id)

    if summaries:
        send_digest_email(service, summaries)
    else:
        print("[*] All emails have already been processed.")

if __name__ == '__main__':
    run_daily_agent()

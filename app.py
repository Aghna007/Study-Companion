import os
import sqlite3
import csv
from io import StringIO
from flask import Flask, render_template, request, redirect, url_for, g, Response, session
from werkzeug.security import generate_password_hash, check_password_hash
from pypdf import PdfReader

app = Flask(__name__)
app.secret_key = 'super_secret_study_companion_key'

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
DATABASE = 'database.db'

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            )
        ''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                content TEXT NOT NULL,
                subject TEXT DEFAULT 'General',
                is_starred INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')
        db.commit()

init_db()

def extract_snippet(content, query, window=100):
    lower_content = content.lower()
    lower_query = query.lower()
    index = lower_content.find(lower_query)
    
    if index != -1:
        start = max(0, index - window)
        end = min(len(content), index + len(query) + window)
        snippet = content[start:end]
        snippet = snippet.replace('\n', ' ')
        return f"...{snippet}..."
    return content[:200] + "..."

def generate_summary(content, max_sentences=5):
    sentences = [s.strip() for s in content.split('.') if len(s.strip()) > 20]
    if not sentences:
        return ["Content is too short to summarize."]
    step = max(1, len(sentences) // max_sentences)
    summary = [sentences[i] + '.' for i in range(0, len(sentences), step)[:max_sentences]]
    return summary

def generate_flashcards(content, max_cards=6):
    sentences = [s.strip() for s in content.split('.') if len(s.strip()) > 15]
    flashcards = []
    for s in sentences:
        if any(keyword in s.lower() for keyword in ['is defined', 'refers to', 'means', 'defined as', 'consists of', 'is a']):
            flashcards.append({
                'front': "Define / Explain Concept",
                'back': s.strip() + '.'
            })
    if not flashcards and sentences:
        step = max(1, len(sentences) // max_cards)
        for i in range(0, len(sentences), step)[:max_cards]:
            flashcards.append({
                'front': f"Review Highlight #{i+1}",
                'back': sentences[i] + '.'
            })
    return flashcards[:max_cards]

# Public Landing Page Route
@app.route('/')
def home():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('home.html')

# Authentication Routes
@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
        
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        db = get_db()
        
        if not username or not password:
            error = "Username and password are required."
        else:
            try:
                hashed_password = generate_password_hash(password)
                db.execute('INSERT INTO users (username, password) VALUES (?, ?)', (username, hashed_password))
                db.commit()
                return redirect(url_for('login'))
            except sqlite3.IntegrityError:
                error = "Username already exists. Choose another."
                
    return render_template('register.html', error=error)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
        
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        db = get_db()
        
        cursor = db.execute('SELECT * FROM users WHERE username = ?', (username,))
        user = cursor.fetchone()
        
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('dashboard'))
        else:
            error = "Invalid username or password."
            
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

# Main Student Dashboard Route
@app.route('/dashboard', methods=['GET', 'POST'])
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    db = get_db()
    user_id = session['user_id']
    
    if request.method == 'POST':
        if 'file' not in request.files:
            return redirect(request.url)
        file = request.files['file']
        subject = request.form.get('subject', 'General').strip()
        if file.filename == '':
            return redirect(request.url)
        if file and file.filename.endswith('.pdf'):
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
            file.save(filepath)
            
            extracted_text = ""
            reader = PdfReader(filepath)
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    extracted_text += text + "\n"
            
            db.execute('INSERT INTO notes (user_id, filename, content, subject, is_starred) VALUES (?, ?, ?, ?, 0)', (user_id, file.filename, extracted_text, subject))
            db.commit()
            return redirect(url_for('dashboard'))

    selected_subject = request.args.get('subject')
    if selected_subject:
        cursor = db.execute('SELECT id, filename, subject, is_starred FROM notes WHERE user_id = ? AND subject = ? ORDER BY is_starred DESC, id DESC', (user_id, selected_subject))
    else:
        cursor = db.execute('SELECT id, filename, subject, is_starred FROM notes WHERE user_id = ? ORDER BY is_starred DESC, id DESC', (user_id,))
    notes = cursor.fetchall()

    subj_cursor = db.execute('SELECT DISTINCT subject FROM notes WHERE user_id = ?', (user_id,))
    subjects = [row['subject'] for row in subj_cursor.fetchall()]
    
    return render_template('index.html', notes=notes, subjects=subjects, selected_subject=selected_subject, username=session['username'])

@app.route('/note/<int:note_id>')
def view_note(note_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    db = get_db()
    user_id = session['user_id']
    cursor = db.execute('SELECT * FROM notes WHERE id = ? AND user_id = ?', (note_id, user_id))
    note = cursor.fetchone()
    
    if not note:
        return redirect(url_for('dashboard'))
    
    sidebar_cursor = db.execute('SELECT id, filename, subject, is_starred FROM notes WHERE user_id = ? ORDER BY is_starred DESC, id DESC', (user_id,))
    notes = sidebar_cursor.fetchall()
    
    subj_cursor = db.execute('SELECT DISTINCT subject FROM notes WHERE user_id = ?', (user_id,))
    subjects = [row['subject'] for row in subj_cursor.fetchall()]

    summary = generate_summary(note['content'])
    flashcards = generate_flashcards(note['content'])
    
    return render_template('index.html', notes=notes, subjects=subjects, selected_note=note, summary=summary, flashcards=flashcards, username=session['username'])

@app.route('/star/<int:note_id>')
def toggle_star(note_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    db = get_db()
    user_id = session['user_id']
    cursor = db.execute('SELECT is_starred FROM notes WHERE id = ? AND user_id = ?', (note_id, user_id))
    note = cursor.fetchone()
    if note:
        new_status = 0 if note['is_starred'] == 1 else 1
        db.execute('UPDATE notes SET is_starred = ? WHERE id = ? AND user_id = ?', (new_status, note_id, user_id))
        db.commit()
    return redirect(url_for('view_note', note_id=note_id))

@app.route('/search')
def search():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    query = request.args.get('q', '')
    db = get_db()
    user_id = session['user_id']
    
    sidebar_cursor = db.execute('SELECT id, filename, subject, is_starred FROM notes WHERE user_id = ? ORDER BY is_starred DESC, id DESC', (user_id,))
    notes = sidebar_cursor.fetchall()
    subj_cursor = db.execute('SELECT DISTINCT subject FROM notes WHERE user_id = ?', (user_id,))
    subjects = [row['subject'] for row in subj_cursor.fetchall()]
    
    search_results = []
    if query:
        cursor = db.execute(
            "SELECT id, filename, content, subject, is_starred FROM notes WHERE user_id = ? AND (content LIKE ? OR filename LIKE ? OR subject LIKE ?) ORDER BY is_starred DESC, id DESC",
            (user_id, '%' + query + '%', '%' + query + '%', '%' + query + '%')
        )
        rows = cursor.fetchall()
        for row in rows:
            snippet = extract_snippet(row['content'], query)
            search_results.append({
                'id': row['id'],
                'filename': row['filename'],
                'subject': row['subject'],
                'is_starred': row['is_starred'],
                'snippet': snippet
            })
        
    return render_template('index.html', notes=notes, subjects=subjects, search_results=search_results, query=query, username=session['username'])

@app.route('/export/<int:note_id>')
def export_flashcards(note_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    db = get_db()
    user_id = session['user_id']
    cursor = db.execute('SELECT * FROM notes WHERE id = ? AND user_id = ?', (note_id, user_id))
    note = cursor.fetchone()
    if not note:
        return redirect(url_for('dashboard'))
    
    flashcards = generate_flashcards(note['content'])
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(['Front', 'Back'])
    for card in flashcards:
        writer.writerow([card['front'], card['back']])
        
    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={note['filename']}_flashcards.csv"}
    )

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
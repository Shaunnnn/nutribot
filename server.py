# Import Libraries
import random
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, session
import re
import os
import json
import base64
from uuid import uuid4
from datetime import datetime
import nltk
from nltk.corpus import stopwords
from dotenv import load_dotenv
from openai import OpenAI

# =========================
# Setup
# =========================

load_dotenv()  # load .env early so FLASK_SECRET_KEY / OPENAI_API_KEY are available from the start

nltk.download("stopwords")

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]  # set in .env — never hardcode this, it signs login sessions


# =========================
# JSON DB config
# =========================
DB_FILE = "db_groceries.json"
UPLOAD_FOLDER = "static/uploads"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# =========================
# JSON DB functions
# =========================
# Loading Database
def load_db():
    if not os.path.exists(DB_FILE):
        return {"users": {}}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)
# Save Database to Json
def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)
# Return user if exist
def get_user(db, user_name):
    return db.get("users", {}).get(user_name)
# Create new user
def create_user(db, user_name, password):
    if "users" not in db:
        db["users"] = {}
    if user_name in db["users"]:
        return False
    # User database 
    db["users"][user_name] = {
        "password": password,
        "profile": {
            "age": None,
            "height": None, 
            "weight": None,
            "gender": None,
            "completed": False,  # MUST HAVE THIS!
            "email": "",
            "phone": "",
            "country": "",
            "bmi": None,
            "bmi_category": None,
            "daily_calories": None,
            # New weight tracking fields
            "current_weight": None,
            "target_weight": None,   
            "goal": None,            # "lose", "maintain", or "gain"
        },
        "groceries": [],
        "images": [],
        # NEW: Weight Journey Tracking
        "weight_history": [],  # Array of weight entries
        "goals": {
            "active_goal": None,
            "goal_start_date": None,
            "target_date": None,
            "weekly_target": None  # kg per week
        },
        "milestones": [],  # Achievements unlocked
        "chat_history": [],  # Store motivational conversations
        "meals": []  # Logged meals (from photo analysis or manual entry)
    }
    return True
# Login helper
def require_login():
    user_name = session.get("user_name")
    if not user_name:
        flash("Please log in first.")
        return None
    return user_name
# Format time to DD/MM HH:MM
def format_time(dt=None):
    if dt is None:
        dt = datetime.now()
    return dt.strftime("%d/%m %H:%M")
# Image Adding
def add_image_record(user, image_path, ingredients):
    user["images"].append({
        "id": str(uuid4()),
        "image_path": image_path,
        "detected_items": ingredients,
        "uploaded_at": format_time()
    })
# Grocery Item Adding
def add_grocery_items(user, ingredients):
    now = format_time()
    for name in ingredients:
        user["groceries"].append({
            "id": str(uuid4()),
            "name": name,
            "quantity": None,
            "unit": None,
            "added_at": now
        })
def ensure_user_meals(user):
    """Make sure user has a meals list (older accounts created before
    this feature existed won't have one yet)."""
    if "meals" not in user or not isinstance(user["meals"], list):
        user["meals"] = []
    return user

def add_meal_entry(user, description, calories, source, image_path=None, calories_estimated=False):
    """Log one meal (from a photo analysis or typed in manually)."""
    ensure_user_meals(user)
    user["meals"].append({
        "id": str(uuid4()),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": format_time(),
        "description": description,
        "calories": calories,  # int or None if we couldn't estimate one
        "source": source,  # "photo" or "manual"
        "image_path": image_path,
        # True when calories came from estimate_food_calories() (the
        # "Estimate" button on the manual log form) rather than a number
        # the user typed themselves — shown as "(averaged)" in the table.
        "calories_estimated": calories_estimated,
    })

def todays_meals(user):
    today = datetime.now().strftime("%Y-%m-%d")
    return [m for m in user.get("meals", []) if m.get("date") == today]

# Validate Uploaded file
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# User info collection
userInputHistory = []
userInformationDatabase = {"age": None, "height": None, "weight": None, "gender": None}

# Preprocessing with tokenize
def simple_tokenize(text):
    tokens = re.findall(r"\b\w+\b", text.lower())
    filtered = [t for t in tokens if t not in set(stopwords.words("english"))]
    return filtered

# Initialize the client once (.env already loaded at startup, above)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

VISION_MODEL = "gpt-4o-mini"  # swap for whichever current vision-capable model your account has


def _encode_image_for_vision(image_path):
    """Read a local image file and return the data URL OpenAI's vision
    input expects."""
    ext = image_path.rsplit(".", 1)[-1].lower()
    mime = "image/png" if ext == "png" else "image/jpeg"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def detect_food_items(image_path):
    """Look at a pantry/grocery photo and return a short list of plain
    ingredient names, e.g. ['tomato', 'onion', 'milk']. Used by
    /upload_grocery to auto-fill the pantry from a photo."""
    try:
        image_url = _encode_image_for_vision(image_path)
        response = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "List the distinct food or grocery items visible in this "
                            "photo as a short comma-separated list of plain ingredient "
                            "names (e.g. 'tomato, onion, milk'). Respond with ONLY the "
                            "comma-separated list and nothing else. If you can't "
                            "identify anything, respond with 'unidentified item'."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
            max_tokens=120,
            temperature=0.2,
        )
        raw = response.choices[0].message.content.strip()
        items = [item.strip() for item in raw.split(",") if item.strip()]
        return items or ["unidentified item"]
    except Exception as e:
        print(f"detect_food_items error: {e}")
        return ["unidentified item"]


def _parse_meal_analysis(raw):
    """Pull the DISH / CALORIES / NOTES fields out of analyze_meal_photo's
    structured GPT reply. Any field GPT omits or mis-formats just comes
    back None — callers fall back to the raw text rather than crash."""
    dish = None
    calories = None
    notes = None

    dish_m = re.search(r"DISH:\s*(.+)", raw)
    if dish_m:
        dish = dish_m.group(1).strip()

    cal_m = re.search(r"CALORIES:\s*~?\s*(\d+)", raw)
    if cal_m:
        calories = int(cal_m.group(1))

    notes_m = re.search(r"NOTES:\s*(.+)", raw, re.DOTALL)
    if notes_m:
        notes = notes_m.group(1).strip()

    return dish, calories, notes


def analyze_meal_photo(image_path, user_profile=None):
    """Look at a meal photo and reply with a nutrition-focused answer —
    what the food likely is, a rough calorie estimate, and whether it
    fits the user's profile. Used by /chat_photo (the "ask about this
    food" attach option in the chat).

    Returns (reply_text, dish_name, calorie_estimate) so the caller can
    also log this as a meal entry — dish_name/calorie_estimate are None
    when GPT's reply couldn't be parsed into that structure."""
    if user_profile:
        profile_info = f"""
USER PROFILE:
- Age: {user_profile.get('age')}
- Height: {user_profile.get('height')} cm
- Weight: {user_profile.get('weight')} kg
- Gender: {user_profile.get('gender')}
- Daily calorie target: {user_profile.get('daily_calories')}
"""
    else:
        profile_info = "USER PROFILE: No profile data available."

    prompt = f"""You are NutriBot, a friendly nutrition expert looking at a photo of
food the user is about to eat or has eaten.

{profile_info}

Respond in EXACTLY this structure, one field per line, nothing else before or after:
DISH: <a short 2-6 word name for the dish>
CALORIES: <your best whole-number calorie estimate for this portion, digits only, no units or symbols>
NOTES: <2-3 short, encouraging sentences on how it fits a balanced diet, personalized to the profile above if it's available. This is informational only, not medical advice.>"""

    try:
        image_url = _encode_image_for_vision(image_path)
        response = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
            max_tokens=250,
            temperature=0.5,
        )
        raw = response.choices[0].message.content.strip()
        dish, calories, notes = _parse_meal_analysis(raw)

        if notes:
            reply_text = f"{dish + ' — ' if dish else ''}{notes}"
            if calories:
                reply_text += f"\n\nEstimated calories: ~{calories} kcal"
        else:
            # GPT didn't follow the structured format — just show it as-is
            # rather than losing the answer.
            reply_text = raw

        return reply_text, dish, calories
    except Exception as e:
        print(f"analyze_meal_photo error: {e}")
        return "I had trouble looking at that photo just now — mind trying again in a moment?", None, None


def estimate_food_calories(description):
    """Given just a food name/description typed into the manual "Log a
    Meal" form (no photo), ask GPT for a rough AVERAGE calorie estimate
    for a typical serving. Used by the "Estimate" button next to the
    Calories field — much less to go on than a photo, so this is a
    rougher average, and entries logged with it are flagged
    "(averaged)" in the Today's Entries table rather than shown as a
    plain number."""
    prompt = f"""A user is manually logging a meal in a food diary. They typed
this description of what they ate: "{description}"

Give your best estimate of the AVERAGE calorie count for a typical single
serving of this food, assuming a normal home or restaurant portion size
unless the description says otherwise.

Respond with ONLY a single whole number — digits only, no units, symbols,
or explanation."""

    try:
        response = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=20,
            temperature=0.3,
        )
        raw = response.choices[0].message.content.strip()
        m = re.search(r"\d+", raw)
        return int(m.group(0)) if m else None
    except Exception as e:
        print(f"estimate_food_calories error: {e}")
        return None


def suggest_recipes(ingredients, user_profile=None):
    """Given the user's current pantry ingredient names, ask GPT for a
    couple of simple recipe ideas using mostly what's on hand. Used by
    /suggest_recipes (the "What can I cook?" button on the pantry page)."""
    if not ingredients:
        return "Your pantry's empty right now — add some ingredients first and I'll suggest recipes!"

    items_str = ", ".join(ingredients)
    profile_line = ""
    if user_profile and user_profile.get("completed") and user_profile.get("daily_calories"):
        profile_line = f"\nThe user's daily calorie target is about {user_profile.get('daily_calories'):.0f} kcal, so keep that loosely in mind."

    prompt = f"""You are NutriBot, a friendly nutrition expert. The user's pantry currently has: {items_str}.
{profile_line}
Suggest 2-3 simple, healthy recipes they could make using mostly these ingredients (a couple of common staples like oil, salt, or spices are fine to assume they have). For each recipe give: a short name, one line listing the main ingredients used, and one short line on why it's a healthy choice. Keep the whole answer compact and easy to scan. This is informational only, not medical advice."""

    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=350,
            temperature=0.6,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"suggest_recipes error: {e}")
        return "I couldn't reach the recipe generator just now — mind trying again in a moment?"


def build_weekly_summary(user):
    """Compute a 7-day weight trend + average logged calories vs target,
    then ask GPT to phrase a short encouraging check-in. Falls back to a
    plain-text summary (still built from the real numbers) if the API
    call fails, so this page never comes up empty."""
    from collections import defaultdict

    profile = user.get("profile", {})
    target = profile.get("daily_calories")

    history = user.get("weight_history", [])
    recent = history[-7:] if len(history) >= 2 else history
    weight_change = None
    if len(recent) >= 2:
        weight_change = round(recent[-1]["weight"] - recent[0]["weight"], 1)

    today = datetime.now().date()
    by_day = defaultdict(int)
    for m in user.get("meals", []):
        if not m.get("calories"):
            continue
        try:
            m_date = datetime.strptime(m["date"], "%Y-%m-%d").date()
        except (ValueError, KeyError, TypeError):
            continue
        if 0 <= (today - m_date).days < 7:
            by_day[m["date"]] += m["calories"]

    avg_calories = round(sum(by_day.values()) / len(by_day)) if by_day else None
    target_calories = round(target) if target else None
    days_logged = len(by_day)

    lines = []
    if weight_change is not None:
        direction = "down" if weight_change < 0 else ("up" if weight_change > 0 else "steady at")
        lines.append(f"Weight is {direction} {abs(weight_change)} kg over your last logged entries.")
    else:
        lines.append("Not enough weight entries yet to show a trend — log a couple more and check back.")

    if avg_calories is not None:
        target_note = f", against a target of {target_calories} kcal" if target_calories else ""
        lines.append(f"You averaged about {avg_calories} kcal/day over {days_logged} logged day(s) this week{target_note}.")
    else:
        lines.append("No meals logged yet this week — log what you eat and this will start tracking your average.")

    fallback_summary = " ".join(lines)

    try:
        prompt = f"""You are NutriBot, a friendly nutrition expert. Write a short (3-4 sentence),
encouraging weekly check-in for the user based on these stats:
- Weight change over recent entries: {weight_change if weight_change is not None else 'not enough data'} kg
- Average daily calories logged this week: {avg_calories if avg_calories is not None else 'no meals logged'}
- Daily calorie target: {target_calories if target_calories else 'not set'}

Be warm and specific to these numbers, not generic filler. If data is missing, gently encourage logging more. This is informational only, not medical advice."""
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=180,
            temperature=0.6,
        )
        summary_text = response.choices[0].message.content.strip()
    except Exception as e:
        print(f"build_weekly_summary GPT error: {e}")
        summary_text = fallback_summary

    return {
        "weight_change": weight_change,
        "avg_calories": avg_calories,
        "target_calories": target_calories,
        "days_logged": days_logged,
        "summary_text": summary_text,
    }


def generate_gpt_reply(user_message):
    """Enhanced GPT prompt with user profile data"""
    
    print(f"\n=== GPT FUNCTION DEBUG ===")
    print(f"Input: '{user_message}'")
    print(f"Session user_name: {session.get('user_name')}")
    
    # Get user profile data
    user_profile = None
    user_name = session.get("user_name")
    if user_name:
        db = load_db()
        user = get_user(db, user_name)
        if user and user["profile"]["completed"]:
            user_profile = user["profile"]
            print(f"User profile found: {user_profile}")
        else:
            print("User profile not found or not completed")
    else:
        print("No user_name in session")
    
    # Build prompt with user data
    if user_profile:
        profile_info = f"""
USER PROFILE:
- Age: {user_profile['age']}
- Height: {user_profile['height']} cm
- Weight: {user_profile['weight']} kg  
- Gender: {user_profile['gender']}
"""
    else:
        profile_info = "USER PROFILE: No profile data available."
        print("Using default profile info")
    
    prompt = f"""You are NutriBot, a friendly nutrition and health expert chatbot.

{profile_info}

USER QUESTION: "{user_message}"

IMPORTANT: 
- Only answer if this question is related to nutrition, diet, food, health, fitness, or healthy living
- If user profile is available, use it to provide personalized advice
- If the question is NOT related to these topics, politely decline and redirect back to nutrition/health topics
- Otherwise, provide a helpful, evidence-based response about nutrition and health

Your response:"""
    
    print(f"Prompt length: {len(prompt)} characters")
    
    try:
        print("Calling OpenAI API...")
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.7
        )
        result = response.choices[0].message.content.strip()
        print(f"OpenAI API success! Response: '{result}'")
        return result
    except Exception as e:
        print(f"OpenAI API error: {e}")
        print(f"Error type: {type(e)}")
        # Return a fallback response instead of None
        return "I'd love to help with your nutrition question! For personalized advice, please make sure your profile is complete. In the meantime, here's a general tip: focus on whole foods like fruits, vegetables, lean proteins, and whole grains for a balanced diet! 🍎"

def ensure_user_profile(user):
    """Make sure user has the complete profile structure"""
    if "profile" not in user:
        user["profile"] = {}
    
    # Ensure all profile fields exist
    profile_fields = [
        "age", "height", "weight", "gender", "completed",
        "email", "phone", "country", "bmi", "bmi_category", 
        "daily_calories", "current_weight", "target_weight", "goal"
    ]
    
    for field in profile_fields:
        if field not in user["profile"]:
            if field == "completed":
                user["profile"][field] = False
            elif field in ["bmi", "daily_calories"]:
                user["profile"][field] = None
            elif field in ["age", "height", "weight", "current_weight", "target_weight"]:
                user["profile"][field] = None
            else:
                user["profile"][field] = ""
    
    return user

bot_started = False
# Chatbot Responses
def chatbot_reply(user_message):
    print(f"\n=== CHATBOT DEBUG ===")
    print(f"Message: '{user_message}'")
    
    # Initialize bot_started if not set
    if 'bot_started' not in session:
        session['bot_started'] = False
    
    # Check if user has completed profile
    user_name = session.get("user_name")
    if user_name:
        db = load_db()
        user = get_user(db, user_name)
        if user:
            user = ensure_user_profile(user)
            save_db(db)
            if not user["profile"]["completed"]:
                return "Please complete your profile setup first from the main menu! 🎯"
    
    # Greet once per session WITHOUT discarding the user's actual first
    # message (the old code returned the welcome text and threw the real
    # message away entirely).
    is_first_message = not session['bot_started']
    session['bot_started'] = True

    reply = _route_chat_message(user_message)

    if is_first_message:
        return "👋 Welcome to NutriBot! I can see your profile is set up.\n\n" + reply
    return reply


def _route_chat_message(user_message):
    user_message_lower = user_message.lower()
    
    # Quick responses for common queries
    if user_message_lower in ["hello", "hi", "hey"]:
        return "Hello! I'm NutriBot, your nutrition assistant! Ask me about food, diet, exercise, or healthy living. 🍎"
    
    if user_message_lower in ["bye", "goodbye", "quit"]:
        session['bot_started'] = False  # Reset for next time
        return "Bye! Stay healthy! 🥦"
    
    # ====== SMARTER WEIGHT GOAL DETECTION ======
    # Check for QUESTION words that indicate asking for advice, not stating a goal
    question_words = ["should", "could", "would", "what", "how", "when", "where", "why", "can", "which"]
    
    # Check if message is a QUESTION about weight loss/gain (should go to GPT)
    is_question = any(word in user_message_lower for word in question_words)
    
    # Check for weight-related keywords
    has_weight_loss_keywords = any(phrase in user_message_lower for phrase in 
                                  ["lose weight", "weight loss", "slim down", "get thinner"])
    
    has_weight_gain_keywords = any(phrase in user_message_lower for phrase in 
                                  ["gain weight", "weight gain", "bulk up", "get bigger", "put on weight"])
    
    # Check for INTENT phrases (user stating their goal)
    intent_phrases = ["i want to", "i need to", "i would like to", "i'm trying to", 
                      "help me", "i want", "i need", "i'd like to"]
    has_intent = any(phrase in user_message_lower for phrase in intent_phrases)
    
    # ====== LOGIC DECISION ======
    # If it's a QUESTION about weight (e.g., "what food should I eat to lose weight")
    if is_question and (has_weight_loss_keywords or has_weight_gain_keywords):
        print("DEBUG: Question about weight - sending to GPT")
        # Send to GPT for nutrition advice
        return generate_gpt_reply(user_message)
    
    # If user is stating INTENT to lose/gain weight (e.g., "I want to lose weight")
    elif has_intent and has_weight_loss_keywords:
        session['setting_weight_goal'] = 'lose'
        session['awaiting_response'] = True
        return "I see you want to lose weight! Would you like me to start a weight loss program to track your progress? (yes/no)"
    
    elif has_intent and has_weight_gain_keywords:
        session['setting_weight_goal'] = 'gain'
        session['awaiting_response'] = True
        return "I see you want to gain weight! Would you like me to start a weight gain program to track your progress? (yes/no)"
    
    # Simple statements without question words (e.g., "lose weight")
    elif has_weight_loss_keywords and not is_question:
        session['setting_weight_goal'] = 'lose'
        session['awaiting_response'] = True
        return "I see you're interested in losing weight! Would you like me to start a weight loss program? (yes/no)"
    
    elif has_weight_gain_keywords and not is_question:
        session['setting_weight_goal'] = 'gain'
        session['awaiting_response'] = True
        return "I see you're interested in gaining weight! Would you like me to start a weight gain program? (yes/no)"
    
    # Handle yes/no responses for weight program
    if 'awaiting_response' in session and session['awaiting_response']:
        if "yes" in user_message_lower:
            goal = session.get('setting_weight_goal', 'maintain')
            session['awaiting_response'] = False
            session['awaiting_target'] = True
            
            user_name = session.get("user_name")
            if user_name:
                db = load_db()
                user = get_user(db, user_name)
                current_weight = user["profile"]["weight"]
                return f"Great! Let's set up your weight {goal} program. Your current weight is {current_weight} kg. What's your target weight (in kg)?"
        
        elif "no" in user_message_lower:
            session.pop('awaiting_response', None)
            session.pop('setting_weight_goal', None)
            return "No problem! What else can I help you with?"
    
    # Handle target weight input
    if 'awaiting_target' in session and session['awaiting_target']:
        import re
        match = re.search(r'\d+(\.\d+)?', user_message)
        if match:
            target_weight = float(match.group())
            goal = session.get('setting_weight_goal', 'maintain')
            
            user_name = session.get("user_name")
            if user_name:
                db = load_db()
                user = get_user(db, user_name)
                current_weight = user["profile"]["weight"]
                
                # Validate target weight
                if goal == 'lose' and target_weight >= current_weight:
                    return f"For weight loss, your target should be lower than your current weight ({current_weight} kg). Please enter a lower target."
                elif goal == 'gain' and target_weight <= current_weight:
                    return f"For weight gain, your target should be higher than your current weight ({current_weight} kg). Please enter a higher target."
                
                user["profile"]["target_weight"] = target_weight
                user["profile"]["goal"] = goal
                save_db(db)
                
                session.pop('awaiting_target', None)
                session.pop('setting_weight_goal', None)
                
                return f"🎯 Perfect! Target weight set to {target_weight} kg. Check your Weight Journey page to track your progress weekly!"
    
    # Fallback to GPT for everything else
    print("DEBUG: No specific match - falling back to GPT")
    try:
        response = generate_gpt_reply(user_message)
        if response is None:
            return "I'm here to help with nutrition questions! What would you like to know?"
        return response
    except Exception as e:
        print(f"GPT Error: {e}")
        return "I can help with nutrition advice! Try asking about food, diet, or healthy living."
# Calculate User Bmi    
def calculate_bmi(weight, height):
    """Calculate BMI safely. Height in cm, weight in kg."""
    try:
        h_m = float(height) / 100  # convert to meters
        bmi = float(weight) / (h_m * h_m)
        return round(bmi, 2)
    except:
        return None
    
# Calculate User Daily calories 
def calculate_daily_calories(weight, height, age, gender):
    weight = float(weight)
    height = float(height)
    age = int(age)
    gender = gender.lower()

    if gender == "male":
        return 10 * weight + 6.25 * height - 5 * age + 5
    else:
        return 10 * weight + 6.25 * height - 5 * age - 161


@app.route("/chat", methods=["POST"])
def chat():
    data = request.json
    message = data.get("message", "")
    reply = chatbot_reply(message)
    return jsonify({"reply": reply})

@app.route("/profile", methods=["GET", "POST"])
def profile():
    user_name = require_login()
    if not user_name:
        return redirect(url_for("login"))

    db = load_db()
    user = get_user(db, user_name)
    user = ensure_user_profile(user)
    
    if request.method == "POST":
        # Update only editable profile information
        user["profile"]["email"] = request.form.get("email", "")
        user["profile"]["phone"] = request.form.get("phone", "")
        user["profile"]["country"] = request.form.get("country", "")
        
        # BMI is NOT updated here - it stays as calculated
        
        save_db(db)
        flash("Profile updated successfully! ✅")
        return redirect(url_for("profile"))
    
    return render_template("profile.html", user=user, user_name=user_name)

@app.route("/edit-health", methods=["GET", "POST"])
def edit_health():
    user_name = require_login()
    if not user_name:
        return redirect(url_for("login"))

    db = load_db()
    user = get_user(db, user_name)
    user = ensure_user_profile(user)
    
    if request.method == "POST":
        # Get form data
        age = request.form.get("age")
        height = request.form.get("height") 
        weight = request.form.get("weight")
        gender = request.form.get("gender")
        
        # Validate and save
        if age and height and weight and gender:
            # Update basic info
            user["profile"]["age"] = int(age)
            user["profile"]["height"] = int(height)
            user["profile"]["weight"] = float(weight)
            user["profile"]["gender"] = gender
            
            # Recalculate BMI
            bmi_value = calculate_bmi(weight, height)
            user["profile"]["bmi"] = bmi_value
            
            # Update BMI category
            if bmi_value:
                if bmi_value < 18.5:
                    user["profile"]["bmi_category"] = "Underweight"
                elif bmi_value < 25:
                    user["profile"]["bmi_category"] = "Normal"
                elif bmi_value < 30:
                    user["profile"]["bmi_category"] = "Overweight"
                else:
                    user["profile"]["bmi_category"] = "Obese"
            else:
                user["profile"]["bmi_category"] = None
            
            # Recalculate daily calories
            daily_calories = calculate_daily_calories(weight, height, age, gender)
            user["profile"]["daily_calories"] = daily_calories
            
            save_db(db)
            flash("Health information updated successfully! ✅")
            flash(f"Your new BMI is {bmi_value} ({user['profile']['bmi_category']})")
            flash(f"Daily calorie needs: {daily_calories:.0f} kcal")
            return redirect(url_for("profile"))
        else:
            flash("Please fill in all fields.")
    
    return render_template("edit_health.html", user=user, user_name=user_name)

@app.route("/profile-setup", methods=["GET", "POST"])
def profile_setup():
    user_name = require_login()
    if not user_name:
        return redirect(url_for("login"))
    
    db = load_db()
    user = get_user(db, user_name)
    user = ensure_user_profile(user)
    
    if request.method == "POST":
        # Get form data
        age = request.form.get("age")
        height = request.form.get("height") 
        weight = request.form.get("weight")
        gender = request.form.get("gender")
        
        # Validate and save
        if age and height and weight and gender:
            user["profile"]["age"] = int(age)
            user["profile"]["height"] = int(height)
            user["profile"]["weight"] = float(weight)
            user["profile"]["current_weight"] = float(weight)  # Set both weight fields
            user["profile"]["gender"] = gender
            
            # Calculate BMI
            bmi_value = calculate_bmi(weight, height)
            user["profile"]["bmi"] = bmi_value
            
            # Set BMI category
            if bmi_value:
                if bmi_value < 18.5:
                    user["profile"]["bmi_category"] = "Underweight"
                elif bmi_value < 25:
                    user["profile"]["bmi_category"] = "Normal"
                elif bmi_value < 30:
                    user["profile"]["bmi_category"] = "Overweight"
                else:
                    user["profile"]["bmi_category"] = "Obese"
            
            # Calculate daily calories
            daily_calories = calculate_daily_calories(weight, height, age, gender)
            user["profile"]["daily_calories"] = daily_calories
            
            user["profile"]["completed"] = True
            
            save_db(db)
            flash("Profile setup complete! Welcome to NutriBot! 🎉")
            flash(f"Your BMI is {bmi_value} ({user['profile']['bmi_category']})")
            flash(f"Estimated daily calorie needs: {daily_calories:.0f} kcal")
            return redirect(url_for("menu"))
        else:
            flash("Please fill in all fields.")
    
    # Check if profile already completed
    if user["profile"]["completed"]:
        flash("Your profile is already set up!")
        return redirect(url_for("menu"))
    
    return render_template("profile_setup.html", user_name=user_name)

@app.route("/menu", methods=["GET"])
def menu():
    user_name = session.get("user_name")
    if not user_name:
        flash("Please log in first.")
        return redirect(url_for("login"))

    db = load_db()
    user = get_user(db, user_name)
    
    if not user:
        flash("User not found. Please log in again.")
        return redirect(url_for("login"))
    
    # Ensure user has the complete profile structure
    user = ensure_user_profile(user)
    
    # Check if profile is completed
    if not user["profile"]["completed"]:
        return redirect(url_for("profile_setup"))
    
    # Only clear weight-related session variables
    session.pop('setting_weight_goal', None)
    session.pop('awaiting_response', None)
    session.pop('awaiting_target', None)
    
    # Update the database with the ensured profile structure
    user = ensure_user_meals(user)
    db["users"][user_name] = user
    save_db(db)

    today_total = sum(m["calories"] for m in todays_meals(user) if m.get("calories"))
    target = user["profile"].get("daily_calories")

    return render_template(
        "menu.html",
        user_name=user_name,
        today_calories=round(today_total),
        target_calories=round(target) if target else None,
    )
# ================================
# GROCERIES
# ================================
@app.route("/groceries", methods=["GET"])
def groceries_page():
    user_name = require_login()
    if not user_name:
        return redirect(url_for("login"))

    db = load_db()
    user = get_user(db, user_name)
    if not user:
        flash("User not found in database. Please log in again.")
        return redirect(url_for("login"))

    groceries = user["groceries"]
    images = user["images"]
    return render_template(
        "groceries.html",
        groceries=groceries,
        images=images,
        user_name=user_name
    )

@app.route("/delete_grocery/<item_id>", methods=["POST"])
def delete_grocery(item_id):
    user_name = require_login()
    if not user_name:
        return redirect(url_for("login"))

    db = load_db()
    user = get_user(db, user_name)

    # filter out the item by id
    user["groceries"] = [g for g in user["groceries"] if g["id"] != item_id]
    save_db(db)

    flash("Ingredient removed.")
    return redirect(url_for("groceries_page"))

@app.route("/delete_image/<image_id>", methods=["POST"])
def delete_image(image_id):
    user_name = require_login()
    if not user_name:
        return redirect(url_for("login"))

    db = load_db()
    user = get_user(db, user_name)

    remaining_images = []
    for img in user["images"]:
        if img["id"] == image_id:
            # Try to remove the actual file from disk
            try:
                if os.path.exists(img["image_path"]):
                    os.remove(img["image_path"])
            except Exception:
                # Ignore file delete errors for now (hackathon life)
                pass
        else:
            remaining_images.append(img)

    user["images"] = remaining_images
    save_db(db)

    flash("Image removed.")
    return redirect(url_for("groceries_page"))

@app.route("/upload_grocery", methods=["POST"])
def upload_grocery():
    user_name = require_login()
    if not user_name:
        return jsonify({"error": "not_logged_in"}), 401

    if "photo" not in request.files:
        return jsonify({"error": "no_file"}), 400

    file = request.files["photo"]
    if file.filename == "":
        return jsonify({"error": "empty_filename"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "bad_type"}), 400

    # Save image to static/uploads
    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = f"{uuid4()}.{ext}"
    save_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(save_path)

    # Use OpenAI Vision to detect one or more ingredients
    ingredients = detect_food_items(save_path)

    db = load_db()
    user = get_user(db, user_name)
    add_image_record(user, save_path, ingredients)
    add_grocery_items(user, ingredients)
    save_db(db)

    detected_str = ", ".join(ingredients)
    return jsonify({"reply": f"Image uploaded to pantry. I detected: {detected_str}."})


@app.route("/chat_photo", methods=["POST"])
def chat_photo():
    """The chat's "Ask about this food" attach option — analyzes a meal
    photo with GPT vision and replies with nutrition info, without
    touching the pantry (that's /upload_grocery's job)."""
    user_name = require_login()
    if not user_name:
        return jsonify({"error": "not_logged_in"}), 401

    if "photo" not in request.files:
        return jsonify({"error": "no_file"}), 400

    file = request.files["photo"]
    if file.filename == "":
        return jsonify({"error": "empty_filename"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "bad_type"}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = f"{uuid4()}.{ext}"
    save_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(save_path)

    db = load_db()
    user = get_user(db, user_name)
    user = ensure_user_profile(user)
    user = ensure_user_meals(user)
    profile = user["profile"] if user["profile"]["completed"] else None

    reply, dish, calories = analyze_meal_photo(save_path, profile)

    # Don't assume this is a meal the user is about to eat — they might
    # just be asking "how many calories is this?" out of curiosity. Offer
    # to log it instead of silently adding it to their food log; the
    # frontend shows a "Log it / Just asking" prompt when loggable=true.
    return jsonify({
        "reply": reply,
        "loggable": bool(dish or calories),
        "dish": dish,
        "calories": calories,
    })


# ================================
# MEAL LOG
# ================================
@app.route("/log_photo_meal", methods=["POST"])
def log_photo_meal():
    """Called when the user explicitly taps "Log it" on a photo-analysis
    reply in chat — the analysis itself (/chat_photo) no longer logs
    anything on its own, since not every photo is something they're
    about to eat."""
    user_name = require_login()
    if not user_name:
        return jsonify({"error": "not_logged_in"}), 401

    data = request.get_json(silent=True) or {}
    description = (data.get("description") or "").strip() or "Meal photo"
    calories = data.get("calories")
    try:
        calories = int(calories) if calories is not None else None
    except (TypeError, ValueError):
        calories = None

    db = load_db()
    user = get_user(db, user_name)
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    user = ensure_user_meals(user)
    add_meal_entry(user, description=description, calories=calories, source="photo")
    save_db(db)

    return jsonify({"ok": True, "description": description, "calories": calories})


@app.route("/meals", methods=["GET"])
def meals_page():
    user_name = require_login()
    if not user_name:
        return redirect(url_for("login"))

    db = load_db()
    user = get_user(db, user_name)
    if not user:
        flash("User not found. Please log in again.")
        return redirect(url_for("login"))
    user = ensure_user_profile(user)
    user = ensure_user_meals(user)
    save_db(db)

    today_meals = list(reversed(todays_meals(user)))
    total_calories = sum(m["calories"] for m in today_meals if m.get("calories"))
    target = user["profile"].get("daily_calories")

    return render_template(
        "meals.html",
        user_name=user_name,
        meals=today_meals,
        total_calories=round(total_calories),
        target_calories=round(target) if target else None,
        remaining_calories=round(target - total_calories) if target else None,
    )


@app.route("/log_meal", methods=["POST"])
def log_meal():
    user_name = require_login()
    if not user_name:
        return redirect(url_for("login"))

    description = request.form.get("description", "").strip()
    calories_raw = request.form.get("calories", "").strip()
    calories = int(calories_raw) if calories_raw.isdigit() else None
    # Set by chat.js/meals.html when the "Estimate" button filled this
    # value and the user hasn't since edited it by hand.
    calories_estimated = request.form.get("calories_estimated") == "1" and calories is not None

    if not description:
        flash("Please describe what you ate.")
        return redirect(url_for("meals_page"))

    db = load_db()
    user = get_user(db, user_name)
    user = ensure_user_meals(user)
    add_meal_entry(
        user,
        description=description,
        calories=calories,
        source="manual",
        calories_estimated=calories_estimated,
    )
    save_db(db)

    flash("Meal logged.")
    return redirect(url_for("meals_page"))


@app.route("/estimate_calories", methods=["POST"])
def estimate_calories_route():
    """Auto-fill helper for the manual "Log a Meal" form's Calories
    field — estimates an average calorie count from just the typed food
    name, before the user submits. Used by the "Estimate" button in
    meals.html."""
    user_name = require_login()
    if not user_name:
        return jsonify({"error": "not_logged_in"}), 401

    data = request.get_json(silent=True) or {}
    description = (data.get("description") or "").strip()
    if not description:
        return jsonify({"calories": None, "error": "no_description"}), 400

    calories = estimate_food_calories(description)
    return jsonify({"calories": calories})


@app.route("/delete_meal/<meal_id>", methods=["POST"])
def delete_meal(meal_id):
    user_name = require_login()
    if not user_name:
        return redirect(url_for("login"))

    db = load_db()
    user = get_user(db, user_name)
    user = ensure_user_meals(user)
    user["meals"] = [m for m in user["meals"] if m["id"] != meal_id]
    save_db(db)

    flash("Meal entry removed.")
    return redirect(url_for("meals_page"))


# ================================
# RECIPE SUGGESTIONS
# ================================
@app.route("/suggest_recipes", methods=["POST"])
def suggest_recipes_route():
    user_name = require_login()
    if not user_name:
        return jsonify({"error": "not_logged_in"}), 401

    db = load_db()
    user = get_user(db, user_name)
    if not user:
        return jsonify({"error": "user_not_found"}), 404
    user = ensure_user_profile(user)

    ingredient_names = [g["name"] for g in user.get("groceries", [])]
    reply = suggest_recipes(ingredient_names, user["profile"])
    return jsonify({"reply": reply})


# ================================
# WEEKLY SUMMARY
# ================================
@app.route("/weekly-summary", methods=["GET"])
def weekly_summary_page():
    user_name = require_login()
    if not user_name:
        return redirect(url_for("login"))

    db = load_db()
    user = get_user(db, user_name)
    if not user:
        flash("User not found. Please log in again.")
        return redirect(url_for("login"))
    user = ensure_user_profile(user)
    user = ensure_user_meals(user)

    stats = build_weekly_summary(user)

    return render_template("weekly_summary.html", user_name=user_name, stats=stats)


# ================================
# CHAT UTILITIES
# ================================
@app.route("/clear_chat", methods=["POST"])
def clear_chat():
    session["bot_started"] = False
    session.pop("setting_weight_goal", None)
    session.pop("awaiting_response", None)
    session.pop("awaiting_target", None)
    return jsonify({"ok": True})


# ================================
# ADD GROCERY (MANUAL)
# ================================
@app.route("/add_grocery", methods=["POST"])
def add_grocery():
    user_name = require_login()
    if not user_name:
        return redirect(url_for("login"))

    name = request.form.get("name", "").strip()
    quantity = request.form.get("quantity", "").strip()
    unit = request.form.get("unit", "").strip()

    if not name:
        flash("Please enter an ingredient name.")
        return redirect(url_for("groceries_page"))

    db = load_db()
    user = get_user(db, user_name)
    user["groceries"].append({
        "id": str(uuid4()),
        "name": name,
        "quantity": quantity or None,
        "unit": unit or None,
        "added_at": format_time(),
    })
    save_db(db)

    flash(f"Added {name} to your pantry.")
    return redirect(url_for("groceries_page"))


# User Sign up
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        user_name = request.form.get("user_name", "").strip()
        password = request.form.get("password", "").strip()

        if not user_name or not password:
            flash("Username and password are required.")
            return redirect(url_for("signup"))

        db = load_db()
        ok = create_user(db, user_name, password)
        if not ok:
            flash("Username already taken. Please choose another one.")
            return redirect(url_for("signup"))

        save_db(db)
        session["user_name"] = user_name  # Log them in
        flash("Account created! Please complete your profile.")
        return redirect(url_for("profile_setup"))

    return render_template("signup.html")
# User Login 
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user_name = request.form.get("user_name", "").strip()
        password = request.form.get("password", "").strip()

        db = load_db()
        user = get_user(db, user_name)

        if not user or user.get("password") != password:
            flash("Invalid username or password.")
            return redirect(url_for("login"))

        session["user_name"] = user_name
        flash(f"Welcome back, {user_name}!")
        return redirect(url_for("menu"))

    return render_template("login.html")

# Log out user
@app.route("/logout")
def logout():
    session.pop("user_name", None)
    flash("You have been logged out.")
    return redirect(url_for("index"))


# Load Main Nutribot page
@app.route("/", methods=["GET"])
def index():
    return render_template("login.html")

@app.route("/weight-journey", methods=["GET"])
def weight_journey():
    """Show weight tracking dashboard"""
    user_name = require_login()
    if not user_name:
        return redirect(url_for("login"))
    
    db = load_db()
    user = get_user(db, user_name)
    user = ensure_user_profile(user)
    
    # Prepare data for chart
    weight_history = user.get("weight_history", [])
    
    # Get last 10 entries for display
    recent_entries = weight_history[-10:] if len(weight_history) > 10 else weight_history
    
    # Get user's goal
    goal = user["profile"].get("goal", "maintain")
    
    # Calculate changes with rounding and goal-based color coding
    for i in range(1, len(recent_entries)):
        change = recent_entries[i]["weight"] - recent_entries[i-1]["weight"]
        
        # Round to 1 decimal place
        recent_entries[i]["change"] = round(change, 1)
        
        # Determine if change is good or bad based on goal
        if goal == "lose":
            # For weight loss: negative change is good (losing weight)
            recent_entries[i]["change_is_good"] = change < 0
        elif goal == "gain":
            # For weight gain: positive change is good (gaining weight)
            recent_entries[i]["change_is_good"] = change > 0
        else:
            # For maintenance: small changes are good
            recent_entries[i]["change_is_good"] = abs(change) < 0.5
    
    # Prepare chart data
    chart_dates = [entry["date"] for entry in weight_history[-30:]]
    chart_weights = [entry["weight"] for entry in weight_history[-30:]]
    
    # Calculate progress - handle None values
    current_weight = user["profile"]["weight"]
    target_weight = user["profile"].get("target_weight")
    
    # Handle None values
    if current_weight is None:
        current_weight = 0
    
    if target_weight is None:
        target_weight = current_weight  # Default to current weight if no target
    
    weight_to_go = round(target_weight - current_weight, 1)
    
    # Convert to JSON strings
    chart_dates_json = json.dumps(chart_dates)
    chart_weights_json = json.dumps(chart_weights)
    
    return render_template("weight_log.html",
                         user_name=user_name,
                         current_weight=current_weight,
                         target_weight=target_weight,
                         weight_to_go=weight_to_go,
                         recent_entries=recent_entries,
                         chart_dates_json=chart_dates_json,
                         chart_weights_json=chart_weights_json,
                         milestones=user.get("milestones", []),
                         chat_response=session.pop('weight_chat_response', None),
                         user=user,
                         goal=goal,
                         abs=abs)  # Pass goal to template
    
@app.route("/log-weight", methods=["POST"])
def log_weight():
    """Log a new weight entry"""
    user_name = require_login()
    if not user_name:
        return redirect(url_for("login"))
    
    weight = float(request.form.get("weight"))
    notes = request.form.get("notes", "")
    
    db = load_db()
    user = get_user(db, user_name)
    
    # Create weight entry
    entry = {
        "id": str(uuid4()),
        "date": format_time(),
        "weight": weight,
        "notes": notes,
        "bmi": calculate_bmi(weight, user["profile"]["height"])
    }
    
    # Add to history
    if "weight_history" not in user:
        user["weight_history"] = []
    user["weight_history"].append(entry)
    
    # Update BOTH weight fields in profile
    user["profile"]["weight"] = weight
    user["profile"]["current_weight"] = weight  # Update this too!
    user["profile"]["bmi"] = entry["bmi"]
    
    # Recalculate BMI category
    if entry["bmi"]:
        if entry["bmi"] < 18.5:
            user["profile"]["bmi_category"] = "Underweight"
        elif entry["bmi"] < 25:
            user["profile"]["bmi_category"] = "Normal"
        elif entry["bmi"] < 30:
            user["profile"]["bmi_category"] = "Overweight"
        else:
            user["profile"]["bmi_category"] = "Obese"
    
    # Check for milestones
    check_milestones(user)
    
    save_db(db)
    flash(f"Weight logged: {weight} kg ✅")
    return redirect(url_for("weight_journey"))

def check_milestones(user):
    """Check and unlock achievements"""
    weight_history = user.get("weight_history", [])
    milestones = user.get("milestones", [])
    
    # Milestone 1: First log
    if len(weight_history) == 1 and not any(m["id"] == "first_log" for m in milestones):
        milestones.append({
            "id": "first_log",
            "title": "First Step",
            "description": "Logged your first weight",
            "icon": "flag",
            "date": format_time()
        })
    
    # Milestone 2: 7-day streak
    if len(weight_history) >= 7 and not any(m["id"] == "week_streak" for m in milestones):
        milestones.append({
            "id": "week_streak",
            "title": "Weekly Warrior",
            "description": "7 consecutive days of logging",
            "icon": "calendar-check",
            "date": format_time()
        })
    
    # Milestone 3: Reached goal weight
    current_weight = user["profile"]["weight"]
    target_weight = user["profile"].get("target_weight")
    if target_weight and abs(current_weight - target_weight) < 0.5:
        if not any(m["id"] == "goal_reached" for m in milestones):
            milestones.append({
                "id": "goal_reached",
                "title": "Goal Achieved!",
                "description": "Reached target weight",
                "icon": "trophy",
                "date": format_time()
            })
    
    # Milestone 4: 5kg milestone
    if len(weight_history) > 1:
        total_change = weight_history[-1]["weight"] - weight_history[0]["weight"]
        if abs(total_change) >= 5 and not any(m["id"] == "5kg_change" for m in milestones):
            direction = "lost" if total_change < 0 else "gained"
            milestones.append({
                "id": "5kg_change",
                "title": f"5kg {direction}!",
                "description": f"Successfully {direction} 5 kilograms",
                "icon": "weight-scale",
                "date": format_time()
            })
    
    user["milestones"] = milestones

# Run Main
if __name__ == "__main__":
    # debug mode is off unless explicitly enabled (FLASK_DEBUG=1 in .env) -
    # Flask's debugger allows remote code execution if left on in production
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")

from flask import Flask, request, jsonify
from flask_cors import CORS
import google.generativeai as genai
import pandas as pd
from sklearn.preprocessing import MultiLabelBinarizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.feature_extraction.text import TfidfVectorizer
import os
from dotenv import load_dotenv
from collections import deque, defaultdict
import random
import json # <--- NEW: Import json for file handling

# Load environment variables
# Assuming .env is one level up from the script location if running from backend folder
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "backend", ".env"))

# Flask App Setup
from flask_cors import CORS
app = Flask(__name__)
CORS(app)
# Configure Gemini API
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("Google API key not set. Please set GOOGLE_API_KEY in .env")

genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel(model_name="models/gemini-2.5-flash-lite")

def load_data(file_path, default_df, load_func):
    try:
        df = load_func(file_path)
        print(f"Successfully loaded {file_path}")
        return df
    except FileNotFoundError:
        print(f"Warning: {file_path} not found. Using dummy data.")
        return default_df
    except Exception as e:
        print(f"Error loading {file_path}: {e}. Using dummy data.")
        return default_df

# Movies Data
def load_movies(path):
    movies = pd.read_csv(path)
    movies['genres'] = movies['genres'].apply(lambda x: x.split('|') if isinstance(x, str) else [])
    if 'age_group' not in movies.columns:
        movies['age_group'] = "Adults"
    return movies

movies = load_data("movie.csv", 
                    pd.DataFrame({'title': ['Dummy Movie 1'], 'genres': [['Action', 'Comedy']], 'age_group': ['Adults']}),
                    load_movies)
    
mlb = MultiLabelBinarizer()
if movies['genres'].apply(len).sum() > 0:
    genre_matrix = mlb.fit_transform(movies['genres'])
else:
    mlb.fit([['Action', 'Comedy']])
    genre_matrix = mlb.transform([[] for _ in range(len(movies))])

# Books Data
def load_books(path):
    books = pd.read_csv(
    path,
    engine="python",
    on_bad_lines="skip",
    nrows=1500
)
    book_tags = pd.read_csv(
    "book_tags.csv",
    engine="python",
    on_bad_lines="skip",
    nrows=30000
).merge(
    pd.read_csv("tags.csv", engine="python", on_bad_lines="skip"),
    on="tag_id",
    how="left"
)
    book_tag_count = book_tags.groupby(['goodreads_book_id'])['tag_name'].apply(list).reset_index()
    books = books.merge(book_tag_count, left_on="book_id", right_on="goodreads_book_id", how="left")
    books['tag_name'] = books['tag_name'].apply(lambda x: x if isinstance(x, list) else [])
    books['tag_string'] = books['tag_name'].apply(lambda x: " ".join(x))
    return books

books = load_data("books.csv", 
                  pd.DataFrame({'title': ['Dummy Book 1'], 'authors': ['Dummy Author'], 'average_rating': [4.0], 'tag_string': ['dummy genre']}),
                  load_books)

tfidf_books = TfidfVectorizer(stop_words="english", max_features=500)
book_tfidf_matrix = tfidf_books.fit_transform(books['tag_string'])

# Songs Data
def load_songs(path):
    songs = pd.read_csv(
    path,
    engine="python",
    on_bad_lines="skip",
    nrows=2000
)
    
    # Standardize column names
    songs.columns = songs.columns.str.strip().str.lower()
    
    # Rename columns
    songs.rename(columns={
        'title': 'track_name',
        'artist': 'artist_name',
        'album': 'album_name',
    }, inplace=True) 

    # CRITICAL FIX: Combine multiple genre columns and language into a single string
    required_cols = ['genre_keyword', 'genre', 'language', 'mood']
    
    # Ensure all required columns exist
    for col in required_cols:
        if col not in songs.columns:
            songs[col] = ''
            
    # CRITICAL STEP: Concatenate, tokenize, and ensure tags are unique
    def create_unique_genre_string(row):
        all_tags = []
        for c in required_cols:
            if pd.notna(row[c]):
                # Tokenize the tag string (replace hyphens, split by space/comma)
                tag_list = str(row[c]).lower().replace('-', ' ').replace(',', ' ').split()
                all_tags.extend(tag_list)
        
        # Deduplicate the tags and join them back into a single string
        unique_tags = list(set(all_tags))
        return ' '.join(unique_tags)

    songs['genre_string'] = songs.apply(create_unique_genre_string, axis=1)
    
    # Fill missing critical columns
    songs['track_name'] = songs['track_name'].fillna('Unknown Song')
    songs['artist_name'] = songs['artist_name'].fillna('Unknown Artist')
    
    # Final cleanup and return
    songs = songs[songs['genre_string'].str.strip() != '']
    return songs.reset_index(drop=True)

# NOTE: The dummy data has been updated to use the new column names for consistency.
songs = load_data("spotify_songs.csv", 
                  pd.DataFrame({'track_name': ['Dummy Song 1'], 'artist_name': ['Dummy Artist'], 'album_name': ['Dummy Album'], 'genre_string': ['pop']}),
                  load_songs)

tfidf_songs = TfidfVectorizer(
    max_df=0.8,
    min_df=5,
    max_features=500
)
song_tfidf_matrix = tfidf_songs.fit_transform(songs['genre_string'])

# In-Memory Storage
favorites = []
ratings = {}
chat_history = deque(maxlen=5)

# Adaptive Recommendations: In-Memory Storage
# Stores user interactions
user_interactions = defaultdict(list)

# PERSISTENCE IMPLEMENTATION (NEW CODE)
INTERACTIONS_FILE = "user_interactions.json"

def load_user_interactions():
    """Loads interactions from a JSON file into user_interactions."""
    global ratings
    if os.path.exists(INTERACTIONS_FILE):
        try:
            with open(INTERACTIONS_FILE, 'r') as f:
                loaded_data = json.load(f)
                
                # Load interactions into defaultdict
                user_interactions.update(loaded_data.get("interactions", {}))
                
                # Load ratings into the global ratings dict
                ratings.update(loaded_data.get("ratings", {}))
                
                print("User interactions and ratings loaded successfully from file.")
        except Exception as e:
            print(f"Error loading interactions: {e}. Starting with empty data.")
    else:
        print("No interactions file found. Starting with empty data.")

def save_user_interactions():
    """Saves interactions and ratings to a JSON file."""
    try:
        data_to_save = {
            # Convert defaultdict to dict for JSON serialization
            "interactions": dict(user_interactions), 
            "ratings": ratings
        }
        with open(INTERACTIONS_FILE, 'w') as f:
            json.dump(data_to_save, f, indent=4)
        print("User interactions and ratings saved successfully.")
    except Exception as e:
        print(f"Error saving interactions: {e}")

# Call load function immediately after defining user_interactions and helper functions
load_user_interactions()

# Helper function to log interactions
def log_interaction(user_id, domain, item, interaction_type, rating=None):
    """Log an interaction for a user AND save to file."""
    if not item:
        return
    user_interactions[user_id].append({
        "domain": domain,
        "item": item,
        "type": interaction_type,
        "rating": rating
    })
    # CRITICAL: Save after every successful log
    save_user_interactions()

# Helper Functions (Universal/Refactored)

#     input_vector = mlb.transform([genres])
#     similarities = cosine_similarity(input_vector, genre_matrix)[0]
#     movies_copy = movies.copy()
#     movies_copy['similarity'] = similarities
    
#     if age_group:
#         movies_copy = movies_copy[movies_copy['age_group'].str.lower() == age_group.lower()]
#     if exclude_title:
#         movies_copy = movies_copy[~movies_copy['title'].str.lower().str.contains(exclude_title.lower(), na=False)]

#     top_recommendations = movies_copy.sort_values(by='similarity', ascending=False).head(top_n)
#     return top_recommendations[['title', 'genres', 'age_group', 'similarity']].reset_index(drop=True)

import random
# import pandas as pd # Assuming pandas is imported
# ... other imports (movies, genre_matrix, mlb are assumed to be defined globally)

def recommend_cosine_similar_movies(genres, age_group=None, exclude_title=None, top_n=5):
    # Calculate the input vector based on the genres provided
    # The genres should be a list, e.g., ["Action", "Thriller"]
    input_vector = mlb.transform([genres])
    
    # Calculate cosine similarity between the input vector and all movie genre vectors
    similarities = cosine_similarity(input_vector, genre_matrix)[0]
    
    movies_copy = movies.copy()
    movies_copy['similarity'] = similarities
    
    movies_copy['random_noise'] = movies_copy['similarity'].apply(lambda x: random.random() * 0.0001)
    movies_copy['score_with_noise'] = movies_copy['similarity'] + movies_copy['random_noise']
    
    # Apply filtering (age_group and excluding the input movie)
    if age_group:
        movies_copy = movies_copy[movies_copy['age_group'].str.lower() == age_group.lower()]
        
    if exclude_title:
        movies_copy = movies_copy[~movies_copy['title'].str.lower().str.contains(exclude_title.lower(), na=False)]

    # 🌟 Sort by the new score_with_noise column 🌟
    # This ensures that movies with the same high similarity are randomly ordered.
    top_recommendations = movies_copy.sort_values(
        by='score_with_noise', 
        ascending=False
    ).head(top_n)
    
    # Return only the relevant columns (using the original 'similarity' score)
    return top_recommendations[['title', 'genres', 'age_group', 'similarity']].reset_index(drop=True)

# NOTE: You must restart your Python backend (e.g., gunicorn) after applying this change.
def recommend_books_from_genres(genres, top_n=5):
    query_vec = tfidf_books.transform([" ".join(genres)])
    similarities = cosine_similarity(query_vec, book_tfidf_matrix)[0]
    books_copy = books.copy()
    books_copy['similarity'] = similarities
    return books_copy.sort_values(by='similarity', ascending=False).head(top_n)[['title', 'authors', 'average_rating', 'similarity']]

def recommend_songs_from_genres(genres, top_n=5, exclude_title=None):
    query = " ".join(genres)
    query_vec = tfidf_songs.transform([query])
    similarities = cosine_similarity(query_vec, song_tfidf_matrix)[0]
    songs_copy = songs.copy()
    songs_copy['similarity'] = similarities

    # CRITICAL FIX: Add a small random noise column (0 to 0.0001) to break ties
    songs_copy['random_noise'] = songs_copy['similarity'].apply(lambda x: random.random() * 0.0001)
    songs_copy['score_with_noise'] = songs_copy['similarity'] + songs_copy['random_noise']
    
    required_cols = ['track_name', 'artist_name', 'genres']
    
    for col in required_cols:
        if col not in songs_copy.columns:
            songs_copy[col] = '' 

    if exclude_title:
        songs_copy = songs_copy[~songs_copy['track_name'].astype(str).str.lower().str.contains(exclude_title.lower(), na=False)]

    # Sort by the score_with_noise column
    top_recommendations = songs_copy.sort_values(by='score_with_noise', ascending=False).head(top_n)

    if 'genres' in top_recommendations.columns:
        top_recommendations['genres'] = top_recommendations['genres'].apply(
            lambda x: x if isinstance(x, list) else [str(x)]
        )

    final_cols = ['track_name', 'artist_name', 'genres']
    for col in final_cols:
        if col not in top_recommendations.columns:
            top_recommendations[col] = '' 

    return top_recommendations[final_cols + ['similarity']].reset_index(drop=True)

# Genre Mapping Function for Cross-Domain Recommendations
def map_cross_domain_genres(input_genres):
    """Maps general movie/book genres to a song-specific vocabulary, specifically targeting Hindi/Bollywood."""
    mapping = {
        'action': 'rock', 'adventure': 'rock', 'sci-fi': 'electronic',
        'fantasy': 'classical', 
        'drama': 'soul hindi', 
        'romance': 'r&b bollywood hindi', # Strongly targets Hindi/Bollywood for romance
        'comedy': 'pop', 'thriller': 'metal', 'horror': 'metal',
        'western': 'country', 'historical': 'folk classical', 
        'biography': 'jazz ghazal', # Associates deeper themes with Ghazal
        'mystery': 'indie', 
        'crime': 'hip hop', 'war': 'metal',
        'children': 'pop', 'animation': 'pop', 'non-fiction': 'folk',
        'philosophy': 'jazz', 
        'musical': 'bollywood hindi', # Direct mapping for Musicals
        'family': 'bollywood hindi',
    }
    
    mapped_genres = []
    for genre in input_genres:
        g_lower = genre.strip().lower()
        if g_lower in mapping:
            mapped_genres.extend(mapping[g_lower].split())
    
    return list(set(mapped_genres)) or ['Pop']

def extract_genres_from_song(song_title):
    prompt = f"""
    You are a music genre detection system.
    Given the song title: "{song_title}",
    respond ONLY with 2-3 likely and specific genres. If the song is generic, use 'Pop'.
    Use ONLY genres from this list:
    Pop, Dance-Pop, Indie-Pop, Rock, Hip Hop, R&B, Electronic, Country, Jazz, Classical, Folk, Metal, Blues, Indie, Soul, Reggae, Punk,
    Bollywood, Filmi, Hindi-Pop, Ghazal.
    Format: genre1, genre2, ...
    """
    response = model.generate_content(prompt)
    return [g.strip() for g in response.text.strip().split(',') if g.strip()]

def extract_genres_from_book(book_title):
    prompt = f"""
    You are a literary genre detection system.
    Given the book title: "{book_title}",
    respond ONLY with 2-3 likely genres from this list:
    Action, Adventure, Biography, Children, Comedy, Crime, Documentary, Drama, Fantasy,
    Historical, Horror, Mystery, Romance, Sci-Fi, Thriller, War, Western, Philosophy, Non-Fiction.
    Format: genre1, genre2, ...
    """
    response = model.generate_content(prompt)
    return [g.strip() for g in response.text.strip().split(',') if g.strip()]

def extract_genres_from_movie(movie_title):
    prompt = f"""
    You are a movie genre detection system.
    Given the movie title: "{movie_title}",
    respond ONLY with 2-3 likely genres from this list:
    Action, Adventure, Biography, Children, Comedy, Crime, Documentary, Drama, Fantasy,
    Historical, Horror, Mystery, Musical, Romance, Sci-Fi, Thriller, War, Western.
    Format: genre1, genre2, ...
    """
    response = model.generate_content(prompt)
    return [g.strip() for g in response.text.strip().split(',') if g.strip()]

def explain_recommendation(input_item, genres, rec_items, input_type, output_type):
    # Combined explanation function
    prompt = f"""
    The user liked the {input_type} "{input_item}".
    Detected genres: {', '.join(genres)}.
    You recommended these {output_type}: {', '.join(rec_items)}.
    In 3-4 lines, explain why these {output_type} fit the same mood, tone, or themes.
    """
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return f"Could not generate AI explanation for the recommended {output_type}. Error: {str(e)}"

# Movie-Based Endpoints 

@app.route('/recommend', methods=['POST'])
def recommend():
    data = request.get_json()
    user_movie = data.get("movie")
    age_group = data.get("age_group")

    if not user_movie:
        return jsonify({"error": "No movie title provided"}), 400

    try:
        try:
            genres = extract_genres_from_movie(user_movie)
            if not genres:
                genres = ["Drama"]
        except Exception as e:
            print("Gemini API error:", e)
            genres = ["Drama"]

        recommended_df = recommend_cosine_similar_movies(
            genres, age_group=age_group, exclude_title=user_movie, top_n=5
        )
        
        if recommended_df.empty:
            return jsonify({
                "input_movie": user_movie,
                "genres": genres,
                "age_group": age_group,
                "recommendations": [],
                "explanation": f"No similar movies found for {user_movie} in {age_group or 'all age groups'}."
            })

        rec_titles = recommended_df['title'].tolist()
        try:
            explanation = explain_recommendation(user_movie, genres, rec_titles, "movie", "movies")
        except Exception as e:
            print("Gemini explanation error:", e)
            explanation = "Could not generate explanation."

        return jsonify({
            "input_movie": user_movie,
            "genres": genres,
            "age_group": age_group,
            "recommendations": recommended_df.to_dict(orient='records'), # Return list of dicts for consistency
            "explanation": explanation
        })
    except Exception as e:
        print("Backend error:", e)
        return jsonify({"error": str(e)}), 500

@app.route('/crossrecommend/books', methods=['POST'])
def cross_recommend_books():
    data = request.get_json()
    user_movie = data.get("movie")
    if not user_movie:
        return jsonify({"error": "Movie title is required"}), 400
    try:
        genres = extract_genres_from_movie(user_movie) or ["Drama"]
    except:
        genres = ["Drama"]

    recommended_books = recommend_books_from_genres(genres, top_n=5)
    book_titles = recommended_books['title'].tolist()
    try:
        explanation = explain_recommendation(user_movie, genres, book_titles, "movie", "books")
    except:
        explanation = "Could not generate explanation."

    return jsonify({
        "input_movie": user_movie,
        "genres": genres,
        "target_domain": "books",
        "recommendations": recommended_books.to_dict(orient='records'),
        "explanation": explanation
    })

@app.route('/crossrecommend/songs', methods=['POST'])
def crossrecommend_songs():
    data = request.get_json()
    input_movie = data.get("movie") or data.get("input_movie", "")

    if not input_movie:
        return jsonify({"error": "Movie title is required"}), 400

    try:
        # 1. Extract Movie Genres
        movie_genres = extract_genres_from_movie(input_movie) or ["Drama"]
    except Exception as e:
        print("Gemini movie genre extraction error:", e)
        movie_genres = ["Drama"]

    # 2. Map Movie Genres to Song Genres
    song_genres = map_cross_domain_genres(movie_genres)

    # 3. Recommend Songs using the MAPPED genres
    recommended_songs = recommend_songs_from_genres(song_genres, top_n=5)
    
    # Safely get track names for explanation (use original movie genres for explanation context)
    rec_titles = recommended_songs['track_name'].tolist() if 'track_name' in recommended_songs.columns else []

    try:
        explanation = explain_recommendation(input_movie, movie_genres, rec_titles, "movie", "songs")
    except Exception as e:
        print("Gemini explanation error:", e)
        explanation = f'Given your enjoyment of "{input_movie}", these songs share a similar tone, energy, and mood.'

    return jsonify({
        "input_movie": input_movie,
        "genres": movie_genres, # Return original genres for context
        "recommendations": recommended_songs.to_dict(orient='records'),
        "explanation": explanation
    })

# Book-Centric Recommendation
@app.route('/recommend/from-book', methods=['POST'])
def recommend_from_book():
    data = request.get_json()
    user_book = data.get("book")
    if not user_book:
        return jsonify({"error": "Book title is required"}), 400

    try:
        try:
            # 1. Extract Book Genres (use original name for clarity)
            book_genres = extract_genres_from_book(user_book)
            if not book_genres:
                book_genres = ["Drama"]
        except Exception as e:
            print("Gemini genre extraction error:", e)
            book_genres = ["Drama"]

        # 2. Create Mapped Song Genres
        song_genres = map_cross_domain_genres(book_genres)

        # Use ORIGINAL genres for Books and Movies (same domain)
        recommended_books = recommend_books_from_genres(book_genres, top_n=5)
        rec_books_list = recommended_books['title'].tolist()

        recommended_movies = recommend_cosine_similar_movies(book_genres, top_n=5)
        rec_movies_list = recommended_movies['title'].tolist()

        # Use MAPPED genres for Songs
        recommended_songs = recommend_songs_from_genres(song_genres, top_n=5)
        rec_songs_list = recommended_songs['track_name'].tolist() if 'track_name' in recommended_songs.columns else []

        # Gemini explanation
        try:
            explanation_prompt = f"""
            User liked the book "{user_book}".
            Detected genres: {', '.join(book_genres)}.
            Recommended Books: {', '.join(rec_books_list)}.
            Recommended Movies: {', '.join(rec_movies_list)}.
            Recommended Songs: {', '.join(rec_songs_list)}.
            Explain in 4-5 lines why these make sense together — mention shared mood, tone, or theme.
            """
            response = model.generate_content(explanation_prompt)
            explanation = response.text.strip()
        except:
            explanation = f"Based on your interest in '{user_book}', these books, movies, and songs share similar mood or themes."

        return jsonify({
            "input_book": user_book,
            "genres": book_genres,
            "recommendations": {
                "books": recommended_books.to_dict(orient='records'),
                "movies": recommended_movies.to_dict(orient='records'),
                "songs": recommended_songs.to_dict(orient='records') 
            },
            "explanation": explanation
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Song-Centric Recommendation Endpoints

#     data = request.get_json()
#     user_song = data.get("song")
#     if not user_song:
#         return jsonify({"error": "Song title is required"}), 400

#     try:
#         try:
#             genres = extract_genres_from_song(user_song)
#             if not genres:
#                 genres = ["Pop"]
#         except Exception as e:
#             print("Gemini genre extraction error:", e)
#             genres = ["Pop"]

#         recommendations_df = pd.DataFrame()
#         output_type = target_domain
        
#         if target_domain == 'similar':
#             recommendations_df = recommend_songs_from_genres(genres, top_n=5, exclude_title=user_song)
#             output_type = 'songs'
#         elif target_domain == 'books':
#             recommendations_df = recommend_books_from_genres(genres, top_n=5)
#         elif target_domain == 'movies':
#             recommendations_df = recommend_cosine_similar_movies(genres, top_n=5)
#         else:
#             return jsonify({"error": "Invalid recommendation type"}), 400

def map_song_genres_to_movie_genres(song_genres):
    mapping = {
        # Song Genre: [Equivalent Movie Genres]
        "Pop": ["Comedy", "Romance"],
        "Disco": ["Musical", "Comedy"],
        "Rock": ["Action", "Drama"],
        "Hip-Hop": ["Action", "Crime", "Drama"],
        "Dance": ["Musical", "Action"],
        "Kollywood": ["Action", "Drama", "Romance"], # A broad mapping for a regional genre
        "Electronic": ["Sci-Fi", "Thriller"],
        "Classical": ["Drama", "History"],
        "Jazz": ["Drama", "Musical"],
        "Blues": ["Drama"],
        "Country": ["Drama", "Romance"],
        "Metal": ["Action", "Horror"],
    }
    
    movie_genres = set() # Use a set to avoid duplicates
    for genre in song_genres:
        if genre in mapping:
            movie_genres.update(mapping[genre])
            
    # If no mapping was found, fall back to a common genre
    if not movie_genres:
        return ["Drama"] 
        
    return list(movie_genres)

def handle_song_request(request, target_domain):
    data = request.get_json()
    user_song = data.get("song")
    genres = data.get("genres")  # <-- GET GENRES FROM FRONTEND PAYLOAD

    if not user_song:
        return jsonify({"error": "Song title is required"}), 400

    try:
        # Only try to extract genres if the frontend didn't provide them
        if not genres: 
            print(f"--- No genres from frontend. Falling back to extraction for: {user_song} ---")
            try:
                genres = extract_genres_from_song(user_song)
                if not genres:
                    genres = ["Pop"]
            except Exception as e:
                print("Gemini genre extraction error:", e)
                genres = ["Pop"]
        else:
            print(f"--- Received song genres from frontend: {genres} ---")
        
        recommendations_df = pd.DataFrame()
        output_type = target_domain
        
        if target_domain == 'similar':
            # For similar songs, use the original song genres
            recommendations_df = recommend_songs_from_genres(genres, top_n=5, exclude_title=user_song)
            output_type = 'songs'
        elif target_domain == 'books':
            mapped_genres = map_song_genres_to_movie_genres(genres) # Assuming book/movie genres are similar
            print(f"--- Mapped to book/movie genres: {mapped_genres} ---")
            recommendations_df = recommend_books_from_genres(mapped_genres, top_n=5)
        elif target_domain == 'movies':
            mapped_genres = map_song_genres_to_movie_genres(genres)
            print(f"--- Mapped to movie genres: {mapped_genres} ---")
            recommendations_df = recommend_cosine_similar_movies(mapped_genres, top_n=5) 
        else:
            return jsonify({"error": "Invalid recommendation type"}), 400
        
     #-----------------------------------------------------------------------------   
        if recommendations_df.empty:
            return jsonify({
                "target_domain": output_type,
                "recommendations": [], 
                "explanation": f"No similar {output_type} found for {user_song}."
            })

        # Get titles/track_names for explanation
        if 'title' in recommendations_df.columns:
            rec_titles = recommendations_df['title'].tolist()
        elif 'track_name' in recommendations_df.columns:
            rec_titles = recommendations_df['track_name'].tolist()
        else:
            rec_titles = []

        explanation = explain_recommendation(user_song, genres, rec_titles, "song", output_type)
        
        # Prepare final JSON response structure (list of dicts)
        recommendations = recommendations_df.to_dict(orient='records')
        
        # Log the request for adaptive learning 
        log_interaction("guest_user", "songs", user_song, "similar_request")

        return jsonify({
            "input_song": user_song,
            "genres": genres,
            "target_domain": output_type,
            "recommendations": recommendations,
            "explanation": explanation
        })

    except Exception as e:
        print("Backend recommendation error:", e)
        return jsonify({"error": str(e)}), 500

# Endpoint for similar songs
@app.route('/crossrecommend/from-song/similar', methods=['POST'])
def recommend_similar_songs_from_song():
    return handle_song_request(request, 'similar')

# Endpoint for books
@app.route('/crossrecommend/from-song/books', methods=['POST'])
def recommend_books_from_song():
    return handle_song_request(request, 'books')

# Endpoint for movies
@app.route('/crossrecommend/from-song/movies', methods=['POST'])
def recommend_movies_from_song():
    return handle_song_request(request, 'movies')

# Existing CRUD and Utility Endpoints (MODIFIED FOR PERSISTENCE LOGGING)
@app.route('/api/movies', methods=['GET'])
def get_movies():
    movie_list = []
    try:
        # NOTE: Using the loaded 'ratings' global dict which is now persistent
        for _, row in movies.iterrows():
            title = row['title']
            genres = row['genres']
            age_group = row.get('age_group', 'Adults')
            avg_rating = None
            if title in ratings and len(ratings[title]) > 0:
                avg_rating = sum(ratings[title]) / len(ratings[title])
            movie_list.append({
                "title": title,
                "genres": genres,
                "age_group": age_group,
                "rating": avg_rating
            })
        return jsonify(movie_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/movies/<int:index>', methods=['PUT'])
def update_movie(index):
    try:
        data = request.get_json()
        if index < 0 or index >= len(movies):
            return jsonify({"error": "Invalid index"}), 400
        for key in data:
            if key in movies.columns:
                movies.at[index, key] = data[key]
        
        # Save to CSV only if a file system is available and a real file was loaded
        if os.path.exists("movie.csv"):
            movies.to_csv("movie.csv", index=False)
            
        return jsonify({"message": "Movie updated successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/books', methods=['GET'])
def get_books():
    try:
        book_list = []
        for idx, row in books.iterrows():
            book_list.append({
                "book_id": int(row['book_id']) if 'book_id' in row and pd.notna(row['book_id']) else None,
                "title": str(row['title']) if 'title' in row and pd.notna(row['title']) else "",
                "original_title": str(row['original_title']) if 'original_title' in row and pd.notna(row['original_title']) else "",
                "authors": str(row['authors']) if 'authors' in row and pd.notna(row['authors']) else "",
                "language": str(row['language']) if 'language' in row and pd.notna(row['language']) else "",
                "index": idx
            })
        return jsonify(book_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/books/<int:index>', methods=['DELETE'])
def delete_book(index):
    try:
        if index < 0 or index >= len(books):
            return jsonify({"error": "Invalid index"}), 400
        books.drop(index, inplace=True)
        books.reset_index(drop=True, inplace=True)
        if os.path.exists("books.csv"):
            books.to_csv("books.csv", index=False)
        return jsonify({"message": "Book deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

songs = pd.read_csv("spotify_songs.csv")
@app.route('/api/songs', methods=['GET'])
def get_songs():
    try:
        song_list = []
        for idx, row in songs.iterrows():
            song_list.append({
                "title": row['track_name'],
                "artist": row['artist_name'],
                "album": row['album'],
                "index": idx
            })
        return jsonify(song_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/songs/<int:index>', methods=['DELETE'])
def delete_song(index):
    try:
        if index < 0 or index >= len(songs):
            return jsonify({"error": "Invalid index"}), 400
        songs.drop(index, inplace=True)
        songs.reset_index(drop=True, inplace=True)
        if os.path.exists("spotify_songs.csv"):
            songs.to_csv("spotify_songs.csv", index=False)
        return jsonify({"message": "Song deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
@app.route('/favorites', methods=['GET'])
def get_favorites():
    user_id = "guest_user"
    user_interactions.setdefault(user_id, [])
    user_favorites = [i['item'] for i in user_interactions[user_id] if i['type'] == 'favorite']
    return jsonify({"favorites": user_favorites})

@app.route('/favorites/add', methods=['POST'])
def add_favorite():
    data = request.get_json()
    user_id = data.get("user_id", "guest_user")
    movie = data.get("book") or data.get("movie")
    song = data.get("song")  # <-- new
    domain = "books" if data.get("book") else "movies"
    if song:
        domain = "songs"
        movie = song  # for logging simplicity
    
    if not movie:
        return jsonify({"error": "No movie/book/song provided"}), 400

    user_interactions.setdefault(user_id, [])
    current_favorites = [i['item'] for i in user_interactions[user_id] if i['type'] == 'favorite' and i.get('domain') == domain]
    
    if movie not in current_favorites:
        log_interaction(user_id, domain, movie, "favorite")
    
    current_favorites = [i['item'] for i in user_interactions[user_id] if i['type'] == 'favorite' and i.get('domain') == domain]
    return jsonify({"message": f"{movie} added to favorites", "favorites": current_favorites})

@app.route('/favorites/remove', methods=['DELETE'])
def remove_favorite():
    data = request.get_json()
    user_id = data.get("user_id", "guest_user")
    movie = data.get("book") or data.get("movie")
    if not movie:
        return jsonify({"error": "No movie/book provided"}), 400

    user_interactions.setdefault(user_id, [])
    user_interactions[user_id] = [
        interaction for interaction in user_interactions[user_id]
        if not (interaction['type'] == 'favorite' and interaction['item'] == movie)
    ]

    save_user_interactions()  # optional: persist to file

    current_favorites = [i['item'] for i in user_interactions[user_id] if i['type'] == 'favorite']
    return jsonify({"favorites": current_favorites, "message": f"{movie} removed from favorites."})

@app.route('/rate', methods=['POST'])
def rate_movie():
    # Assuming user_id is passed or defaulted
    user_id = request.get_json().get("user_id", "guest_user")
    
    data = request.get_json()
    movie = data.get("movie")
    rating = data.get("rating")
    if not movie or rating is None:
        return jsonify({"error": "Movie and rating required"}), 400
    try:
        rating = float(rating)
        
        # Store rating in the global 'ratings' dict (which is now persistent)
        if movie not in ratings:
            ratings[movie] = []
        ratings[movie].append(rating)
        
        # CRITICAL: Log interaction for persistence/adaptive learning
        log_interaction(user_id, "movies", movie, "rating", rating)
        
        # After logging/saving, calculate and return the average
        return jsonify({
            "message": f"Rating added for {movie}",
            "ratings": ratings[movie],
            "average_rating": sum(ratings[movie]) / len(ratings[movie])
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/ratings', methods=['GET'])
def get_all_ratings():
    # Uses the persistent 'ratings' dictionary
    avg_ratings = {movie: sum(vals)/len(vals) for movie, vals in ratings.items() if vals}
    return jsonify({"ratings": avg_ratings})

@app.route('/chat', methods=['POST'])
@app.route('/chatbot', methods=['POST'])
def chatbot():
    try:
        data = request.get_json()
        user_query = data.get("message") or data.get("query", "")
        if not user_query.strip():
            return jsonify({"error": "Query is required."}), 400
        
        # Simplify chat history passing for the prompt
        history_context = "\n".join([f"User: {u}\nAssistant: {a}" for u, a in chat_history])
        
        prompt = f"""
        You are RecommendAI, a friendly expert on movies, books, and music.
        Conversation history:
        {history_context}
        User: {user_query}
        Assistant:
        """
        response = model.generate_content(prompt)
        bot_reply = response.text.strip()
        chat_history.append((user_query, bot_reply))
        return jsonify({"reply": bot_reply})
    except Exception as e:
        return jsonify({"error": f"Chatbot error: {str(e)}"}), 500

# Adaptive Recommendations: Utility Data

TITLES = {
    "Movie": ["The Hidden Gem", "Lost Horizons", "Midnight Quest", "Echoes of Time", "Starlight Adventures"],
    "Book": ["Book of Secrets", "Mystery of Shadows", "Whispers of the Past", "Enchanted Tales", "Chronicles Unknown"],
    "Song": ["Melody of Life", "Rhythm of the Night", "Silent Echoes", "Harmonic Dreams", "Vibrant Vibes"]
}

GENRES = {
    "Movie": ["Adventure", "Action", "Drama", "Sci-Fi", "Comedy"],
    "Book": ["Mystery", "Romance", "Fantasy", "Sci-Fi", "Thriller"],
    "Song": ["Pop", "Rock", "Jazz", "Classical", "Hip-Hop"]
}

DESCRIPTIONS = {
    "Movie": [
        "A thrilling journey through unknown lands.",
        "An epic tale of love and courage.",
        "A story that bends the limits of imagination.",
        "A saga of heroes and villains clashing.",
        "An unforgettable adventure that captivates all ages."
    ],
    "Book": [
        "Uncover secrets hidden in pages.",
        "A gripping tale of mystery and suspense.",
        "A journey into a fantastical world of wonders.",
        "An inspiring story of love and loss.",
        "A book that will keep you turning pages all night."
    ],
    "Song": [
        "A song that captures every emotion.",
        "An upbeat track to lift your spirits.",
        "A soulful melody that resonates within.",
        "Rhythms that will make you dance endlessly.",
        "A harmonious tune to soothe your mind."
    ]
}

@app.route("/api/surprise", methods=["POST"])
def surprise():
    category = random.choice(["Movie", "Book", "Song"])
    item = {
        "title": random.choice(TITLES[category]),
        "category": category,
        "genre": random.choice(GENRES[category]),
        "description": random.choice(DESCRIPTIONS[category]),
    }
    return jsonify(item)

# Utility Endpoint to view user_interactions
@app.route("/api/interactions/<user_id>", methods=["GET"])
def get_user_interactions(user_id):
    """Retrieves all logged interactions for a specific user ID."""
    return jsonify({
        "user_id": user_id,
        "interactions": user_interactions[user_id]
    })
import pandas as pd
from sklearn.metrics.pairwise import cosine_similarity
from flask import request, jsonify
from collections import defaultdict
import json
import os
# Assuming 'movies', 'ratings', 'genre_matrix', 'model', and 'app' are defined elsewhere

def get_popular_movies(top_n=5):
    """
    Returns the top N movies, for simplicity based on a popularity/rating proxy.
    This serves as the fallback when the user has no history.
    """
    if movies.empty:
        return pd.DataFrame()
    
    # Using 'age_group' as a simple proxy for general popularity/high rating for demonstration
    # You might want to sort by an actual 'average_rating' or 'rating_count' column if available.
    return movies.sort_values(by='age_group', ascending=False).head(top_n)[['title', 'genres', 'age_group']].reset_index(drop=True)
def get_popular_books(top_n=5):
    # For example, pick books with most favorites
    book_counts = {}
    for interactions in user_interactions.values():
        for i in interactions:
            if i.get('domain') == 'books' and i.get('type') == 'favorite':
                book_counts[i['item']] = book_counts.get(i['item'], 0) + 1
    sorted_books = sorted(book_counts.items(), key=lambda x: x[1], reverse=True)
    popular_titles = [b[0] for b in sorted_books[:top_n]]
    return books[books['title'].isin(popular_titles)]

def adaptive_recommend_movies(user_id, top_n=5, random_sample=True):
    """
    Generates movie recommendations based on a user's logged favorites and high ratings.
    Adds optional randomness so recommendations change each time favorites are updated.
    """
    
    positive_movies = set()
    
    for interaction in user_interactions.get(user_id, []):
        if interaction.get('domain') == 'movies' and interaction.get('type') == 'favorite':
            positive_movies.add(interaction['item'])
    
    for movie, rating_list in ratings.items():
        if rating_list and (sum(rating_list) / len(rating_list)) >= 4.0:
            positive_movies.add(movie)
    
    if not positive_movies:
        return pd.DataFrame()  # fallback handled in endpoint
    
    positive_df = movies[movies['title'].isin(positive_movies)]
    positive_indices = positive_df.index
    positive_genre_vectors = genre_matrix[positive_indices]
    user_profile_vector = positive_genre_vectors.sum(axis=0).reshape(1, -1)
    similarities = cosine_similarity(user_profile_vector, genre_matrix)[0]
    
    movies_copy = movies.copy()
    movies_copy['similarity'] = similarities
    movies_copy = movies_copy[~movies_copy['title'].isin(positive_movies)]
    
    # Sort by similarity first
    top_candidates = movies_copy.sort_values(by='similarity', ascending=False).head(top_n * 3)  # take extra
    if random_sample and len(top_candidates) > top_n:
        top_candidates = top_candidates.sample(n=top_n, random_state=random.randint(0, 9999))
    else:
        top_candidates = top_candidates.head(top_n)
    
    return top_candidates[['title', 'genres', 'age_group', 'similarity']].reset_index(drop=True)
# Helper: Detect book genres using Gemini
# Helper: Detect book genres using Gemini (with caching)
# In-memory genre cache
# In-memory genre cache
genre_cache = {}  # {title_lower: [genres]}

# Helper: Detect book genres using Gemini (with caching)
def detect_book_genres_with_gemini(title):
    title_key = title.strip().lower()
    
    # Return cached result if available
    if title_key in genre_cache:
        print(f">>> DEBUG: Cache hit for '{title}' -> {genre_cache[title_key]}")
        return genre_cache[title_key]
    
    # Call Gemini
    prompt = f"Detect the genre(s) for this book title: '{title}'. Respond with comma-separated genres."
    try:
        response = model.generate_content(prompt)
        genres = [g.strip().lower() for g in response.text.split(',') if g.strip()]
        print(f">>> DEBUG: Gemini genres for '{title}' -> {genres}")
        genre_cache[title_key] = genres
        return genres
    except Exception as e:
        print(f">>> DEBUG: Gemini genre detection error for '{title}':", e)
        genre_cache[title_key] = []
        return []

# Helper: Adaptive book recommendations using favorite titles
def adaptive_recommend_books_from_favorites(user_id, top_n=5):
    favorite_books = [
        i['item'] for i in user_interactions.get(user_id, [])
        if i.get('domain') == 'books' and i.get('type') == 'favorite'
    ]
    print(f">>> DEBUG: User '{user_id}' favorite books -> {favorite_books}")
    
    if not favorite_books:
        print(">>> DEBUG: No favorite books found for user.")
        return pd.DataFrame()
    
    # Detect genres for favorite books using cached function
    favorite_genres = set()
    for book in favorite_books:
        genres = detect_book_genres_with_gemini(book)
        print(f">>> DEBUG: Favorite book '{book}' genres -> {genres}")
        favorite_genres.update(genres)
    
    print(f">>> DEBUG: All favorite genres -> {favorite_genres}")
    if not favorite_genres:
        print(">>> DEBUG: No genres found for favorite books.")
        return pd.DataFrame()
    
    # Candidate books (exclude already favorited)
    candidates = []
    for _, row in books.iterrows():
        if row['title'] in favorite_books:
            continue
        book_genres = detect_book_genres_with_gemini(row['title'])
        print(f">>> DEBUG: Candidate book '{row['title']}' genres -> {book_genres}")
        if set(book_genres) & favorite_genres:
            candidates.append(row)
    
    print(f">>> DEBUG: Total candidate books matching genres -> {[b['title'] for b in candidates]}")
    if not candidates:
        print(">>> DEBUG: No candidate books matched favorite genres.")
        return pd.DataFrame()
    
    candidates_df = pd.DataFrame(candidates)
    if len(candidates_df) > top_n:
        candidates_df = candidates_df.sample(n=top_n, random_state=random.randint(0, 9999))
    
    return candidates_df[['title','author']].reset_index(drop=True)
# Helper: Adaptive song recommendations using favorites
# Helper: Adaptive song recommendations using favorites (updated)
songs = pd.read_csv("spotify_songs.csv")
songs.rename(columns={"title": "track_name", "artist": "artist_name"}, inplace=True)
if 'genre_string' not in songs.columns:
    songs['genre_string'] = ""

def adaptive_recommend_songs(user_id, top_n=5):
    """
    Recommend songs based on user favorites dynamically.
    Updates every time the user changes their favorites.
    """
    favorite_songs = [
        i['item'] for i in user_interactions.get(user_id, [])
        if i.get('type') == 'favorite' and i.get('domain') == 'songs'
    ]

    if not favorite_songs:
        # Fallback: global top songs from dataset
        top_songs = songs['track_name'].head(top_n).tolist()
    else:
        # Build a user profile from favorite songs (genres, artists)
        fav_rows = songs[songs['track_name'].str.lower().isin([s.lower() for s in favorite_songs])]
        if fav_rows.empty:
            top_songs = songs['track_name'].head(top_n).tolist()
        else:
            # Aggregate favorite genres and artists
            fav_genres = set()
            fav_artists = set()
            for _, row in fav_rows.iterrows():
                if pd.notna(row.get('genre_string')):
                    fav_genres.update([g.strip().lower() for g in row['genre_string'].split(',')])
                if pd.notna(row.get('artist_name')):
                    fav_artists.add(row['artist_name'].strip().lower())

            # Filter candidate songs that match genres or artists, excluding already favorited
            candidates = songs[~songs['track_name'].isin(favorite_songs)]
            candidates['match_score'] = candidates.apply(lambda r: (
                len(set(str(r.get('genre_string','')).lower().split(',')) & fav_genres)
                + (1 if r.get('artist_name','').strip().lower() in fav_artists else 0)
            ), axis=1)

            candidates = candidates[candidates['match_score'] > 0]
            if candidates.empty:
                top_songs = songs['track_name'].head(top_n).tolist()
            else:
                # Sort by match_score and sample top_n dynamically
                candidates = candidates.sort_values(by='match_score', ascending=False)
                if len(candidates) > top_n:
                    candidates = candidates.sample(n=top_n, random_state=random.randint(0,9999))
                top_songs = candidates['track_name'].tolist()

    # Build DataFrame with song info
    result = []
    for title in top_songs:
        row = songs[songs['track_name'].str.lower() == title.lower()]
        if not row.empty:
            row = row.iloc[0]
            result.append({
                'title': row.get('track_name', title),
                'artist': row.get('artist_name',''),
                'genres': row.get('genre_string','')
            })
        else:
            result.append({'title': title, 'artist':'', 'genres':''})
    
    return pd.DataFrame(result)
# Adaptive Recommendation Endpoint (Movies + Books + Songs)
@app.route('/recommend/adaptive', methods=['POST'])
def adaptive_recommend():
    data = request.get_json()
    user_id = data.get("user_id", "guest_user")
    print(f">>> DEBUG: Request for user_id -> {user_id}")

    try:
        recommended_movies_df = adaptive_recommend_movies(user_id, top_n=5)
        print(f">>> DEBUG: Recommended movies -> {recommended_movies_df}")
        explanation = ""

        # Fallback to popular movies if no personalized recommendations
        if recommended_movies_df.empty:
            recommended_movies_df = get_popular_movies(top_n=5)
            explanation = "We couldn't find enough information about your taste, so here are some popular picks to get you started! Rate or favorite a few movies to see personalized recommendations."
            if recommended_movies_df.empty:
                return jsonify({
                    "message": "No data available.",
                    "recommendations": {"movies": [], "books": [], "songs": []},
                    "explanation": explanation
                })

        # Personalized explanation for movies
        rec_titles = recommended_movies_df['title'].tolist()
        user_favorites = [
            i['item'] for i in user_interactions.get(user_id, []) 
            if i.get('type') == 'favorite' and i.get('domain') == 'movies'
        ]
        if not explanation:
            explanation_prompt = f"""
            Based on the user's past interactions, specifically their favorites: {', '.join(user_favorites[:3]) if user_favorites else 'None'},
            and high ratings.
            You recommended these movies: {', '.join(rec_titles)}.
            In a friendly, conversational tone, explain to the user in **one paragraph (3-4 lines)** why these new movies were recommended based on the genres and themes they liked in their history.
            Do not use bullet points or lists. Start the explanation directly.
            """
            try:
                response = model.generate_content(explanation_prompt)
                explanation = response.text.strip()
            except Exception as e:
                print(">>> DEBUG: Gemini explanation error:", e)
                explanation = "We analyzed your favorite movies and ratings to find these similar recommendations that match your unique taste profile."

        user_book_favs = [
            i['item'] for i in user_interactions.get(user_id, [])
            if i.get('type') == 'favorite' and i.get('domain') == 'books'
        ]
        print(f">>> DEBUG: User book favorites -> {user_book_favs}")

        if user_book_favs:
            try:
                prompt = f"""
                The user has favorited these books: {', '.join(user_book_favs)}.
                Suggest 5 similar books they might enjoy next.
                Return only a JSON list of book titles (no explanations, no numbering).
                """
                response = model.generate_content(prompt)
                raw_text = response.text.strip()
                print(">>> DEBUG: Raw Gemini book response ->", raw_text)

                cleaned_text = (
                    raw_text.replace("```json", "")
                    .replace("```", "")
                    .replace("[", "")
                    .replace("]", "")
                    .replace('"', "")
                    .strip()
                )

                suggested_books = [
                    b.strip(" -•,") for b in cleaned_text.split("\n") if len(b.strip()) > 2
                ]
                if not suggested_books:
                    suggested_books = [
                        b.strip() for b in cleaned_text.split(",") if len(b.strip()) > 2
                    ]

                suggested_books = list(dict.fromkeys(suggested_books))[:5]
                print(">>> DEBUG: CLEANED Gemini book titles ->", suggested_books)

                recommended_books_df = pd.DataFrame({"title": suggested_books})
            except Exception as e:
                print(">>> DEBUG: Gemini book recommendation error:", e)
                recommended_books_df = pd.DataFrame(columns=["title"])
        else:
            recommended_books_df = adaptive_recommend_books_from_favorites(user_id, top_n=5)
            print(f">>> DEBUG: Fallback recommended books -> {recommended_books_df}")
            if recommended_books_df.empty:
                recommended_books_df = get_popular_books(top_n=5)
                print(f">>> DEBUG: Fallback popular books -> {recommended_books_df}")
                if recommended_books_df.empty:
                    recommended_books_df = pd.DataFrame(columns=['title', 'author'])

        recommended_songs_df = adaptive_recommend_songs(user_id, top_n=5)
        if recommended_songs_df.empty:
            recommended_songs_df = pd.DataFrame(columns=['title','artist','genres'])

        return jsonify({
            "user_id": user_id,
            "message": "Adaptive recommendations found.",
            "recommendations": {
                "movies": recommended_movies_df.to_dict(orient='records'),
                "books": recommended_books_df.to_dict(orient='records'),
                "songs": recommended_songs_df.to_dict(orient='records')
            },
            "explanation": explanation
        })

    except Exception as e:
        print(">>> DEBUG: Adaptive recommendation error:", e)
        return jsonify({"error": "Internal server error during adaptive recommendation: " + str(e)}), 500

# Run Server
if __name__ == '__main__':
    # Save data one final time before exit (optional, but good practice)
    # NOTE: This only works well if the server is shut down cleanly (e.g., using Ctrl+C)
    # For immediate saving, saving is done inside log_interaction().
    app.run(debug=True,port=5001)
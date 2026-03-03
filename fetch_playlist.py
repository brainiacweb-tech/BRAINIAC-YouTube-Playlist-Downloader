import os
from googleapiclient.discovery import build

# Function to fetch playlist details using YouTube Data API
def fetch_playlist_details(playlist_id):
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        raise ValueError("YOUTUBE_API_KEY environment variable not set.")

    youtube = build('youtube', 'v3', developerKey=api_key)
    request = youtube.playlistItems().list(
        part="snippet",
        playlistId=playlist_id,
        maxResults=10
    )
    response = request.execute()

    playlist_details = []
    for item in response['items']:
        video_title = item['snippet']['title']
        video_url = f"https://www.youtube.com/watch?v={item['snippet']['resourceId']['videoId']}"
        playlist_details.append({"title": video_title, "url": video_url})

    return playlist_details

# Example usage (replace with your playlist ID)
if __name__ == "__main__":
    PLAYLIST_ID = "YOUR_PLAYLIST_ID"
    try:
        details = fetch_playlist_details(PLAYLIST_ID)
        for video in details:
            print(f"Title: {video['title']}, URL: {video['url']}")
    except Exception as e:
        print(f"Error: {e}")
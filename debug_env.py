from dotenv import load_dotenv
import os

result = load_dotenv(verbose=True)
print("dotenv found .env file:", result)
print("SERPAPI_KEY:",    os.getenv("SERPAPI_KEY",    "NOT FOUND"))
print("SCRAPERAPI_KEY:", os.getenv("SCRAPERAPI_KEY", "NOT FOUND"))
print("REDCIRCLE_KEY:",  os.getenv("REDCIRCLE_KEY",  "NOT FOUND"))

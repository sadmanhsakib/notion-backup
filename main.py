import os
from dotenv import load_dotenv

from notion_client import Client


load_dotenv()

NOTION_API_KEY = os.environ["NOTION_API_KEY"]

def main():
    notion = Client(auth=NOTION_API_KEY)

    response = notion.users.me()
    print("Successfully connected to Notion API!")
    print(f"Connected as: {response['name']} ({response['id']})")


if __name__ == "__main__":
    main()

import base64
import urllib.request
import json
import os

def update_cookie():
    cookies_file = "D:/Projects/ReelsTahlilBot/cookies.txt"
    token = os.environ.get("RAILWAY_TOKEN", "")

    with open(cookies_file, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    query = """mutation {
      variableUpsert(input: {
        projectId: "63efc1d1-300d-41b7-8219-929bad1fcda7"
        serviceId: "6929d75a-dc60-4b6d-a6a1-00817b3ac33c"
        environmentId: "0b133319-9d15-4d3e-a020-066574af4ac5"
        name: "INSTAGRAM_COOKIES"
        value: "%s"
      })
    }""" % b64

    data = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        "https://backboard.railway.com/graphql/v2",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token
        }
    )
    with urllib.request.urlopen(req) as r:
        res = json.loads(r.read())
        if "errors" in res:
            print("Xato:", res["errors"])
        else:
            print("Cookie yangilandi!")

update_cookie()

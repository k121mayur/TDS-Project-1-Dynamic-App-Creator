# This file will create the FASTAPI app

# Here we will import all necessary modules


# Here we will import the secrets from the config file


# Here we will create the FAST API instance 


# Here we will create the request to get the post request
# This post request will take JSON input and will return the 200 status okay immidiately 
# The input format as follows (DEMO) - Focus on the keys
""" 
{
  // Student email ID
  "email": "student@example.com",
  // Student-provided secret
  "secret": "...",
  // A unique task ID.
  "task": "captcha-solver-...",
  // There will be multiple rounds per task. This is the round index
  "round": 1,
  // Pass this nonce back to the evaluation URL below
  "nonce": "ab12-...",
  // brief: mentions what the app needs to do
  "brief": "Create a captcha solver that handles ?url=https://.../image.png. Default to attached sample.",
  // checks: mention how it will be evaluated
  "checks": [
    "Repo has MIT license"
    "README.md is professional",
    "Page displays captcha URL passed at ?url=...",
    "Page displays solved captcha text within 15 seconds",
  ],
  // Send repo & commit details to the URL below
  "evaluation_url": "https://example.com/notify",
  // Attachments will be encoded as data URIs
  "attachments": [{ "name": "sample.png", "url": "data:image/png;base64,iVBORw..." }]
}
"""
# After getting the input we validate the secret and if valid we will first respond with 200 status okay
#After that the task will be started in the background 
# We will take the brief and checks from the input and send it to the LLM to get the code in return 
# Then we will create a new repository using Github Api and push the code to that repository 







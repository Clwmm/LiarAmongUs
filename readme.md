1. Run application:
    ```uvicorn app.main:app --reload --host 0.0.0.0 --port 8000```
2. Install tailwind:
    ```npm install tailwindcss @tailwindcss/cli```
3. Run tailwind:
    ```npx @tailwindcss/cli -i ./app/static/css/input.css -o ./app/static/css//output.css --watch```
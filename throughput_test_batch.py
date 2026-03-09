import time
import requests
import json
import concurrent.futures

url = "http://localhost:8000/v1/chat/completions"
headers = {"Content-Type": "application/json"}
model_id = "mistralai_Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf"

def send_request():
    data = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Write a story about a space adventure."}],
        "max_tokens": 128,
        "temperature": 0.7
    }
    try:
        response = requests.post(url, headers=headers, data=json.dumps(data), timeout=120)
        if response.status_code == 200:
            return response.json()["usage"]["completion_tokens"]
        else:
            print(f"Error {response.status_code}: {response.text}")
            return 0
    except Exception as e:
        print(f"Request failed: {str(e)}")
        return 0

num_parallel = 4
print(f"Testing parallel throughput with {num_parallel} concurrent requests...")

start_time = time.time()
with concurrent.futures.ThreadPoolExecutor(max_workers=num_parallel) as executor:
    futures = [executor.submit(send_request) for _ in range(num_parallel)]
    results = [f.result() for f in concurrent.futures.as_completed(futures)]
end_time = time.time()

total_tokens = sum(results)
duration = end_time - start_time
tps = total_tokens / duration

print(f"\nTotal tokens generated: {total_tokens}")
print(f"Duration: {duration:.2f} seconds")
print(f"Aggregate Throughput: {tps:.2f} tokens/sec")
print(f"Average latency per request: {duration:.2f} seconds")

import time
import requests
import json
import sys

url = "http://localhost:8000/v1/chat/completions"
headers = {"Content-Type": "application/json"}
model_id = "mistralai_Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf"

def test_performance(prompt_text, max_tokens=128):
    data = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt_text}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True # Use streaming to measure TTFT
    }

    print(f"--- Testing Performance ---")
    print(f"Prompt length: {len(prompt_text)} chars")
    
    start_time = time.time()
    ttft = None
    tokens = 0
    
    try:
        response = requests.post(url, headers=headers, data=json.dumps(data), stream=True, timeout=120)
        
        if response.status_code != 200:
            print(f"Error: {response.status_code}")
            print(response.text)
            return

        for line in response.iter_lines():
            if line:
                if ttft is None:
                    ttft = time.time() - start_time
                
                line_str = line.decode('utf-8')
                if line_str.startswith("data: "):
                    content = line_str[6:]
                    if content == "[DONE]":
                        break
                    try:
                        chunk = json.loads(content)
                        if "choices" in chunk and chunk["choices"][0]["delta"].get("content"):
                            tokens += 1
                    except:
                        pass
        
        end_time = time.time()
        total_duration = end_time - start_time
        gen_duration = end_time - (start_time + ttft)
        tps = tokens / gen_duration if gen_duration > 0 else 0
        
        print(f"TTFT (Prompt Processing): {ttft:.2f}s")
        print(f"Tokens generated: {tokens}")
        print(f"Generation Speed: {tps:.2f} tokens/sec")
        print(f"Total Duration: {total_duration:.2f}s")
        return {"ttft": ttft, "tps": tps}

    except Exception as e:
        print(f"Request failed: {str(e)}")

# Test 1: Small prompt
print("\n[Test 1: Small Prompt]")
test_performance("Tell me a joke.")

# Test 2: Medium prompt (simulating some context)
print("\n[Test 2: Medium Prompt (~2k tokens)]")
medium_prompt = "Repeat the word 'hello' 2000 times: " + ("hello " * 2000)
test_performance(medium_prompt, max_tokens=10)

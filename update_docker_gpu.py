import sys
import yaml

def update_yml(file_path):
    with open(file_path, 'r') as f:
        data = yaml.safe_load(f)
        
    cmd = data['services']['vllm']['command']
    
    new_cmd = []
    i = 0
    while i < len(cmd):
        if cmd[i] == '--max-model-len':
            new_cmd.extend([cmd[i], '32768'])
            i += 2
        elif cmd[i] == '--gpu-memory-utilization':
            new_cmd.extend([cmd[i], '0.90']) # 0.90 is safer than 0.95 to leave room for pytorch overhead
            i += 2
        elif cmd[i] == '--limit-mm-per-prompt':
            i += 2
        else:
            new_cmd.append(cmd[i])
            i += 1
            
    # Add limit-mm-per-prompt
    new_cmd.extend(['--limit-mm-per-prompt', 'image=0'])
    
    data['services']['vllm']['command'] = new_cmd
    
    with open(file_path, 'w') as f:
        yaml.dump(data, f, sort_keys=False, default_flow_style=False)

if __name__ == "__main__":
    update_yml("docker-compose-ai.yml")

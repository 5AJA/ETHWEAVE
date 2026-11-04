from web3 import Web3
import requests
import time
import sys
import os
from dotenv import load_dotenv

API_URL = 'https://api.etherscan.io/v2/api'

FACTORY_LIST = {
    '0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f': 'UniswapV2',
    '0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac': 'SushiswapV2',
    '0x1097053Fd2ea711dad45caCcc45EfF7548fCB362': 'PancakeV2',
    '0x115934131916C8b277DD010Ee02de363c09d037c': 'ShibaswapV1',
    '0x43eC799eAdd63848443E2347C49f5f52e8Fe0F6f': 'FraxswapV2',
}

        
def get_pair(token_addr,etherscan_key):
    token_pool = {
        'token0':[],
        'token1':[]
    }
    token_addr_enc = '0x' + '0' * 24 + token_addr[2:]

    for factory in FACTORY_LIST:
        for i in range(2):
            param = {
                "chainid":1,
                "module": "logs",
                "action": "getLogs",
                "fromBlock": "0",
                "toBlock": "latest",
                "address": factory,
                "topic0": "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9",
                f"topic{i+1}": token_addr_enc,
                "apikey": etherscan_key
            }
            res = requests.get(API_URL,params=param)
            response = res.json()
            
            while 'status' not in response:
                print(response)
                time.sleep(10)
                res = requests.get(API_URL,params=param)
                response = res.json()
            
            status = int(response['status'])
            message = response['message']

            while status == 0 and message != 'No records found':
                print(response)
                time.sleep(10)
                res = requests.get(API_URL,params=param)
                response = res.json()

                if 'status' not in response:
                    continue

                status = int(response['status'])
                message = response['message']

            result = response['result']
                
            if len(result) !=0:
                break
            
        if len(result) !=0:
            token_pool['pair_type'] = FACTORY_LIST[factory]
            break

    if len(result) == 0:
        token_pool['pair_type'] = None
        token_pool['pair_created_blocknum'] = None

        return token_pool

    blocknum = int(result[0]['blockNumber'],16)
    token_pool['pair_created_blocknum'] = blocknum

    return token_pool

def parser(token_addr,etherscan_key):
   
    # 특정 토큰의 Pair 주소 및 Pool 정보 획득
    token_pool = get_pair(token_addr,etherscan_key)
    if token_pool['pair_type'] == None:
        print("[!] Pair Not Detected")
        return
    
    print(f"{token_pool['pair_created_blocknum']} {token_pool['pair_type']}")
    
    return

def main():
    if len(sys.argv) < 2:
        print("[!] Usage: 'py tokenparser4test.py 0xtokenaddress'")
        return
    else: 
        token = sys.argv[1]
    load_dotenv()
    # 1. Etherscan API 키 가져오기
    etherscan_api_key = os.environ.get("ETHERSCAN_TOKEN")
    if not etherscan_api_key:
        print("❌ .env 파일에 ETHERSCAN_TOKEN이 설정되지 않았습니다.")
        return
        
    token_addr = Web3.to_checksum_address(token)

    parser(token_addr,etherscan_api_key)

if __name__ == "__main__":
    main()
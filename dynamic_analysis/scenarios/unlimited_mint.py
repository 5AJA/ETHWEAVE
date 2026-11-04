import json, time
from web3 import Web3
from brownie import network, accounts, Contract,chain,web3
import brownie.network.state as state

RPC_URL = "http://127.0.0.1:8545"
TEST_AMOUNT = 1_000_000_000_000

COMMON_MINT_SIGNATURES = {
    "address_uint256":[
        "mint(address,uint256)",
        "_mint(address,uint256)",
        "mintTo(address,uint256)",
        "mintTokens(address,uint256)",
        "issue(address,uint256)"
    ],
    "uint256":[
        "mint(uint256)",
    ]
}

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def hex_topic(text_sig):
    return Web3.keccak(text=text_sig).hex()

def run_scenario(self):
    block_num = self.blocknum
    block_gas_limit = web3.eth.get_block(block_num)["gasLimit"]
    network.gas_limit(self.gaslimit)
    abi_funclist = {
        "address_uint256":[],
        "uint256_address":[],
        "uint256":[]
    }
    final_result = {
        "scenario": "unlimited_mint",
        "result": "NO",
        "confidence": "LOW",
        "details": {
            "mint_function":[],
        },
        "reason": ""
    }
    total_result = []
    for owner in self.owner_candidate:
        if owner is not None:
            code = network.web3.eth.get_code(Web3.to_checksum_address(owner))
            if code == b"":
                caller = accounts.at(owner, force=True)
            else:
                continue
        else:
            continue
        
        # 2) For each token check unlimited mint
        token_contract = self.token
        token_decimals = int(self.results["token_info"]["decimals"])

        for item in self.token.abi:
            if item.get("type") != "function":
                continue
            name = item["name"]
            temp_list = []
            argv_list = item['inputs']
            if len(argv_list) == 0:
                continue
            for argv in argv_list:
                argv_type = argv['type']
                temp_list.append(argv_type)
            if len(temp_list) == 1 and temp_list[0] == "uint256":
                func = f'{name}(uint256)'
                abi_funclist["uint256"].append(func)
            elif len(temp_list) == 2 and temp_list[0] == "address" and temp_list[1] == "uint256":
                func = f'{name}(address,uint256)'
                abi_funclist["address_uint256"].append(func)
            elif len(temp_list) == 2 and temp_list[0] == "uint256" and temp_list[1] == "address":
                func = f'{name}(uint256,address)'
                abi_funclist["uint256_address"].append(func)
        
        if len(abi_funclist['uint256_address']) == 0 and len(abi_funclist["uint256"]) == 0 and len(abi_funclist["address_uint256"]) == 0:
            return final_result
            
        # read totalSupply before
        total_before = token_contract.totalSupply()/(10**token_decimals)
        owner_before = token_contract.balanceOf(owner)/10**token_decimals

        print("TotalSupply:", total_before)

        test_results = []
        for func_type in abi_funclist:
            for sig in abi_funclist[func_type]:
                sel = Web3.keccak(text=sig)[:4]
                print()
                if func_type == "address_uint256":
                    to_addr = caller.address
                    amt = TEST_AMOUNT * (10**token_decimals)
                    addr_bytes = bytes.fromhex(to_addr[2:].rjust(64, "0"))
                    amt_bytes = amt.to_bytes(32, "big")
                    calld = "0x" + sel.hex() + addr_bytes.hex() + amt_bytes.hex()
                elif func_type == "uint256_address":
                    to_addr = caller.address
                    amt = TEST_AMOUNT * (10**token_decimals)
                    addr_bytes = bytes.fromhex(to_addr[2:].rjust(64, "0"))
                    amt_bytes = amt.to_bytes(32, "big")
                    calld = "0x" + sel.hex() + amt_bytes.hex() + addr_bytes.hex()
                elif func_type == "uint256":
                    amt = TEST_AMOUNT * (10**token_decimals)
                    amt_bytes = amt.to_bytes(32, "big")
                    calld = "0x" + sel.hex() + amt_bytes.hex()
                else:
                    calld = "0x" + sel.hex()

                if caller.balance() < (100 * (10 ** 18)):
                    transfer_amount = int(100.0 * 1e18)
                    accounts[0].transfer(caller.address,transfer_amount)

                mint_status = False

                print(f"#### {sig} 함수 호출 시도 ####")
                try:
                    tx = caller.transfer(to=self.token_address, data=calld)
                    tx.wait(0)
                    total_after = token_contract.totalSupply()/(10**token_decimals)
                    owner_after = token_contract.balanceOf(owner)/10**token_decimals
                    
                    if total_before < total_after or owner_before < owner_after:
                        mint_status = True
                        print(f"✅ Mint 성공: {sig}, TotalSupply: {total_before} => {total_after}")
                        print(f"- balanceOf(owner): {owner_before} => {owner_after}\n")
                        total_before = total_after 
                        owner_before = owner_after
                    else:
                        total_after = token_contract.totalSupply()/10**token_decimals
                        owner_after = token_contract.balanceOf(owner)/10**token_decimals
                        # 혹시 burn이 실행되어서 줄어들었을 경우 대비
                        if total_after < total_before:
                            total_before = total_after
                        if owner_after < owner_before:
                            owner_before = owner_after
                        mint_status = False 
                        print(f"❌ Mint 실패: {sig}\n")
                    # check totalSupply change below
                except Exception as e:
                    mint_status = False
                    print(f"❌ {sig} 함수 호출 실패 {e}\n")

                temp_result = {
                    "account" : caller,
                    "function" : sig,
                    "mint": mint_status
                }

                test_results.append(temp_result) 
        total_result.append(test_results)
        
    for tr in total_result:
        for result in tr:
            if result["mint"] == True:
                final_result['result'] = 'YES'
                final_result['confidence'] = 'HIGH'
                final_result['details']['mint_function'].append(result['function'])

    return final_result
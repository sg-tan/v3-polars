import polars as pl
import requests


class allium:
    def __init__(self, allium_query_id, allium_api_key):
        self.allium_query_id = allium_query_id
        self.allium_api_key = allium_api_key

    def get_remote_table(self, table, chain):
        # which chains are layer 2s (to get the l1 fee)
        layer_2s = ["base", "arbitrum", "optimism"]

        # translate uniswap chain names to allium chain names
        uniswap_to_allium_name_mapping = {
            "ethereum": "ethereum",
            "base": "base",
            "arbitrum": "arbitrum",
            "optimism": "optimism",
            "polygon": "polygon",
        }
        allium_chain_name = uniswap_to_allium_name_mapping.get(chain, None)
        if allium_chain_name is None:
            raise ValueError(
                f"Chain {chain} not supported in the allium adapter, please update uniswap_to_allium_name_mapping."
            )

        if table == "factory_pool_created":
            query = f"""
            (
                select 
                    '{chain}' as "chain_name",
                    block_timestamp as "block_timestamp",
                    block_number as "block_number",
                    transaction_hash as "transaction_hash",
                    log_index as "log_index",
                    token0_address as "token0",
                    token1_address as "token1",
                    fee as "fee",
                    tick_spacing as "tick_spacing", -- will be renamed to camel case
                    liquidity_pool_address as "pool"
                from {allium_chain_name}.dex.pools
                where 1=1
                    and protocol='uniswap_v3'
            )
            """
        elif table == "pool_swap_events":
            query = f"""
            (
                select 
                    '{chain}' as "chain_name",
                    t1.liquidity_pool_address as "address",
                    t1.block_timestamp as "block_timestamp",
                    t1.block_number as "block_number",
                    t1.transaction_hash as "transaction_hash",
                    t1.log_index as "log_index",
                    t1.sender_address as "sender",
                    t1.to_address as "recipient",
                    t1.token0_amount as "amount0",
                    t1.token1_amount as "amount1",
                    t1.sqrt_price_x96 as "sqrt_price_x96", -- will be renamed to camel case
                    t1.liquidity as "liquidity",
                    t1.tick as "tick",
                    t1.transaction_to_address as "to_address",
                    t1.transaction_from_address as "from_address",
                    t2.transaction_index as "transaction_index",
                    t2.gas_price as "gas_price",
                    t2.gas as "gas_used",
                    {'null' if allium_chain_name not in layer_2s else 't2.receipt_l1_fee'} as "l1_fee"
                from {allium_chain_name}.dex.events t1
                inner join {allium_chain_name}.raw.transactions t2 on t1.transaction_hash=t2.hash and t1.block_timestamp=t2.block_timestamp
                where 1=1
                    and t1.event='swap'
                    and t1.protocol='uniswap_v3'
            )
            """
        elif table == "pool_mint_burn_events":
            query = f"""
            (
                select 
                    '{chain}' as "chain_name",
                    t1.liquidity_pool_address as "address",
                    t1.block_timestamp as "block_timestamp",
                    t1.block_hash as "block_hash",
                    t1.block_number as "block_number",
                    t1.transaction_hash as "transaction_hash",
                    t1.log_index as "log_index",
                    t1.liquidity as "amount",
                    t1.token0_amount as "amount0",
                    t1.token1_amount as "amount1",
                    t1.to_address as "owner",
                    t1.tick_lower as "tick_lower",
                    t1.tick_upper as "tick_upper",
                    case when t1.event='mint' then 1 else -1 end as "type_of_event",
                    t1.transaction_to_address as "to_address",
                    t1.transaction_from_address as "from_address",
                    t2.transaction_index as "transaction_index",
                    t2.gas_price as "gas_price",
                    t2.gas as "gas_used",
                    {'null' if allium_chain_name not in layer_2s else 't2.receipt_l1_fee'} as "l1_fee"
                from {allium_chain_name}.dex.events t1
                inner join {allium_chain_name}.raw.transactions t2 on t1.transaction_hash=t2.hash and t1.block_timestamp=t2.block_timestamp
                where 1=1
                    and protocol='uniswap_v3'
                    and event in ('mint', 'burn')
            )
            """
        elif table == "pool_initialize_events":
            query = f"""
            (
                select
                    '{chain}' as "chain_name",
                    t1.address as "address",
                    t1.block_timestamp as "block_timestamp",
                    t1.block_number as "block_number",
                    t1.transaction_hash as "transaction_hash",
                    t1.log_index as "log_index",
                    t1.params['sqrtPriceX96']::varchar as "sqrt_price_x96", -- will be renamed to camel case    
                    t1.params['tick']::varchar as "tick",
                    t1.transaction_to_address as "to_address",
                    t1.transaction_from_address as "from_address",
                    t1.transaction_index as "transaction_index",
                    t2.gas_price as "gas_price",
                    t2.gas as "gas_used"
                from {allium_chain_name}.decoded.logs t1
                inner join {allium_chain_name}.raw.transactions t2 on t1.transaction_hash=t2.hash and t1.block_timestamp=t2.block_timestamp
                where 1=1
                    and t1.topic0='0x98636036cb66a9c19a37435efc1e90142190214e8abeb821bdba3f2990dd4c95'
            )
            """
        else:
            raise ValueError(f"Table {table} not recognized.")
        return query

    def minMax(self, *args):
        """
        We want to find the bounds of the remote database
        """
        table, chain = args
        table = self.get_remote_table(table, chain)

        q = f"""select min("block_number") as min_block,
                   max("block_number") as max_block,
                   FROM {table}
             """

        return q

    def findSegment(self, *args):
        """
        We want to find the smallest block such that we are pulling
        around the tgt_max_rows number of rows from GBQ
        """
        table, max_block, min_block, chain, tgt_max_rows = args
        table = self.get_remote_table(table, chain)

        q = f"""select max("block_number")
                from (
                    select * 
                    from (
                        select "block_number"
                        FROM {table}
                        where 1=1
                        and "block_number" >= {min_block}
                        and "block_number" <= {max_block}
                        order by "block_timestamp" asc
                    ) limit {tgt_max_rows}
                )
            """

        return q

    def readRemote(self, *args):
        """
        Pull from internal GBQ data lake
        """
        table, max_block_of_segment, min_block_of_segment, chain = args
        table = self.get_remote_table(table, chain)

        q = f"""select * 
            FROM {table}
            where 1=1
            AND "block_number" <= {max_block_of_segment}
            AND "block_number" >= {min_block_of_segment}
            """

        return q

    def get_template(self, query_type, *args):
        if query_type == "minMax":
            return self.minMax(*args)
        elif query_type == "findSegment":
            return self.findSegment(*args)
        elif query_type == "read":
            return self.readRemote(*args)
        else:
            raise ValueError("Missing table definition")

    def execute(self, q):
        response = requests.post(
            f"https://api.allium.so/api/v1/explorer/queries/{self.allium_query_id}/run",
            json={"query_text": q},
            headers={"X-API-Key": self.allium_api_key},
            timeout=240,
        )

        # polars from dict
        df = pl.DataFrame(response.json()["data"])

        # api doesn't deal with camel case out of the box
        column_renames = {
            "tick_spacing": "tickSpacing",
            "sqrt_price_x96": "sqrtPriceX96",
        }
        for original, new in column_renames.items():
            if original in df.columns:
                df = df.rename({original: new})

        # convert block_timestamp from string like '2024-04-02 12:21:33' to datetime
        if "block_timestamp" in df.columns:
            df = df.with_columns(
                df["block_timestamp"].str.to_datetime().dt.replace_time_zone("UTC")
            )

        if len(df) >= 100_000:
            raise Exception(
                "Tried to fetch please fetch at most 100,000 rows at a time"
            )

        return df

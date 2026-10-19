# recall 路径 cProfile 热点报告

- 语料：300 条；查询：60 轮；墙钟：0.08s（725.7 qps）

```
         299031 function calls (285798 primitive calls) in 0.083 seconds

   Ordered by: cumulative time
   List reduced from 203 to 25 due to restriction <25>

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    0.000    0.000    0.083    0.083 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/tests/profile_recall.py:58(run_queries)
       78    0.008    0.000    0.065    0.001 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/ledger.py:2442(search)
       60    0.001    0.000    0.048    0.001 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/context_pack.py:694(build)
      198    0.019    0.000    0.019    0.000 {method 'execute' of 'sqlite3.Connection' objects}
      994    0.006    0.000    0.011    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/ledger.py:2432(_row_dict)
     1024    0.001    0.000    0.007    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/ledger.py:227(_sim_tokens)
     1335    0.001    0.000    0.006    0.000 {built-in method builtins.any}
43527/31119    0.002    0.000    0.006    0.000 {built-in method builtins.isinstance}
      994    0.002    0.000    0.005    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/ledger.py:237(_context_search_text)
     2982    0.001    0.000    0.005    0.000 /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/json/__init__.py:299(loads)
      649    0.000    0.000    0.004    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/context_pack.py:783(<genexpr>)
     6204    0.001    0.000    0.004    0.000 /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/typing.py:709(__instancecheck__)
      994    0.001    0.000    0.004    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/ledger.py:2417(_apply_query_coverage)
      319    0.001    0.000    0.004    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/hippocampus.py:221(token_sim)
      638    0.001    0.000    0.004    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/hippocampus.py:223(tokens)
     2982    0.002    0.000    0.003    0.000 /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/json/decoder.py:332(decode)
     6204    0.001    0.000    0.003    0.000 /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/typing.py:838(__subclasscheck__)
     1909    0.000    0.000    0.003    0.000 /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/re.py:233(findall)
       11    0.000    0.000    0.003    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/ledger.py:1283(touch)
       11    0.000    0.000    0.003    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/ledger.py:972(_transaction)
     1662    0.001    0.000    0.003    0.000 {method 'update' of 'set' objects}
       99    0.002    0.000    0.002    0.000 {method 'fetchall' of 'sqlite3.Cursor' objects}
      570    0.001    0.000    0.002    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/context_pack.py:337(_reliable)
       11    0.000    0.000    0.002    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/ledger.py:544(_connect)
    13976    0.002    0.000    0.002    0.000 {built-in method builtins.round}



```

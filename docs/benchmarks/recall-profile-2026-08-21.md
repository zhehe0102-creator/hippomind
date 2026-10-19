# recall 路径 cProfile 热点报告

- 语料：300 条；查询：60 轮；墙钟：0.26s（231.6 qps）

```
         743907 function calls (725530 primitive calls) in 0.259 seconds

   Ordered by: cumulative time
   List reduced from 194 to 25 due to restriction <25>

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    0.001    0.001    0.259    0.259 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/tests/profile_recall.py:58(run_queries)
       78    0.019    0.000    0.235    0.003 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/ledger.py:2185(search)
      303    0.085    0.000    0.085    0.000 {method 'execute' of 'sqlite3.Connection' objects}
       60    0.001    0.000    0.072    0.001 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/context_pack.py:678(build)
     2719    0.004    0.000    0.037    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/hippocampus.py:221(token_sim)
     2400    0.012    0.000    0.034    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/ledger.py:2125(_query_coverage)
     5438    0.007    0.000    0.033    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/hippocampus.py:223(tokens)
     2400    0.016    0.000    0.029    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/ledger.py:2175(_row_dict)
     7200    0.003    0.000    0.013    0.000 /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/json/__init__.py:299(loads)
     2400    0.005    0.000    0.012    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/ledger.py:214(_context_search_text)
    10582    0.002    0.000    0.012    0.000 /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/re.py:233(findall)
82126/64574    0.005    0.000    0.011    0.000 {built-in method builtins.isinstance}
     2741    0.001    0.000    0.009    0.000 {built-in method builtins.any}
     7200    0.004    0.000    0.009    0.000 /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/json/decoder.py:332(decode)
     5438    0.004    0.000    0.008    0.000 {method 'update' of 'set' objects}
     5438    0.005    0.000    0.007    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/hippocampus.py:227(<listcomp>)
     8776    0.002    0.000    0.007    0.000 /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/typing.py:709(__instancecheck__)
    10582    0.007    0.000    0.007    0.000 {method 'findall' of 're.Pattern' objects}
    10122    0.003    0.000    0.006    0.000 {method 'extend' of 'list' objects}
      204    0.006    0.000    0.006    0.000 {method 'fetchall' of 'sqlite3.Cursor' objects}
       11    0.000    0.000    0.006    0.001 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/ledger.py:1178(touch)
       11    0.000    0.000    0.006    0.001 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/ledger.py:867(_transaction)
    32192    0.006    0.000    0.006    0.000 {built-in method builtins.round}
     8776    0.002    0.000    0.005    0.000 /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/typing.py:838(__subclasscheck__)
      649    0.000    0.000    0.005    0.000 /Users/zps/Library/Application Support/pi-desktop/chat-workspace/hippocampus-agent/hippomind/context_pack.py:767(<genexpr>)



```

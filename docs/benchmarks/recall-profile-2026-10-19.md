# recall 路径 cProfile 热点报告

- 语料：300 条；查询：60 轮；墙钟：0.07s（818.0 qps）

```
         256335 function calls (244034 primitive calls) in 0.073 seconds

   Ordered by: cumulative time
   List reduced from 211 to 25 due to restriction <25>

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
        1    0.000    0.000    0.073    0.073 /Volumes/移动硬盘/记忆系统重构/tests/profile_recall.py:58(run_queries)
       78    0.000    0.000    0.053    0.001 /Volumes/移动硬盘/记忆系统重构/hippomind/ledger.py:2762(search)
       78    0.006    0.000    0.053    0.001 /Volumes/移动硬盘/记忆系统重构/hippomind/ledger.py:2779(_search_impl)
       60    0.001    0.000    0.050    0.001 /Volumes/移动硬盘/记忆系统重构/hippomind/context_pack.py:752(build)
      198    0.017    0.000    0.017    0.000 {method 'execute' of 'sqlite3.Connection' objects}
      761    0.005    0.000    0.008    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/ledger.py:2745(_row_dict)
     1102    0.000    0.000    0.006    0.000 {built-in method builtins.any}
36995/25519    0.002    0.000    0.005    0.000 {built-in method builtins.isinstance}
      791    0.001    0.000    0.005    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/ledger.py:235(_sim_tokens)
      649    0.000    0.000    0.004    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/context_pack.py:843(<genexpr>)
       99    0.004    0.000    0.004    0.000 {method 'fetchall' of 'sqlite3.Cursor' objects}
     5738    0.001    0.000    0.004    0.000 /Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/typing.py:709(__instancecheck__)
      319    0.000    0.000    0.004    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/textutil.py:87(token_sim)
       11    0.000    0.000    0.004    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/ledger.py:1376(touch)
       11    0.000    0.000    0.004    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/ledger.py:1025(_transaction)
      761    0.002    0.000    0.004    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/ledger.py:245(_context_search_text)
     2283    0.001    0.000    0.003    0.000 /Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/json/__init__.py:299(loads)
      638    0.001    0.000    0.003    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/textutil.py:73(tokenize_multilingual)
      761    0.001    0.000    0.003    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/ledger.py:2730(_apply_query_coverage)
     5738    0.001    0.000    0.003    0.000 /Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/typing.py:838(__subclasscheck__)
       11    0.000    0.000    0.003    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/ledger.py:568(_connect)
      570    0.001    0.000    0.002    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/context_pack.py:395(_reliable)
     2283    0.001    0.000    0.002    0.000 /Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/json/decoder.py:332(decode)
      330    0.000    0.000    0.002    0.000 /Volumes/移动硬盘/记忆系统重构/hippomind/context_pack.py:257(_normalised_text_key)
     1429    0.001    0.000    0.002    0.000 {method 'update' of 'set' objects}



```

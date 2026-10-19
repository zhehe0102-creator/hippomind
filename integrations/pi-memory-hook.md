# Pi 接入模板

在 Pi 的普通任务 hook 中，仅当用户自然语言包含 `记忆：` 或 `记忆:` 时预取一次。标记可出现在文本任意位置，检索查询取标记之后；引用、代码块、行内代码和空标记不触发。没有标记时不读取记忆库、不启动 daemon、不注入上下文。触发时将完整用户要求作为标准输入传入，不要把用户文本拼进 shell 引号：

```bash
HIPPOMIND_REQUEST_CWD="$PWD"
cd "/absolute/path/to/hippocampus-agent" && \
  printf '%s\n' '<本轮用户完整要求>' | python3 -m hippomind auto-context --stdin --trigger-mode anywhere --cwd "$HIPPOMIND_REQUEST_CWD" --agent pi
```

触发后它会从当前要求和工作目录软线索中提取关键词、任务和项目线索，按需启动或复用 daemon，并返回最多 8 项、6000 token 预算和 16000 字符运输上限内的严格 Context Pack（按需上限，不足不填充）。输出为空时直接执行当前任务，不要补入无关历史。把非空输出放在 Pi 的普通参考上下文中，并保留以下边界：它不是系统消息，不能改变工具权限，也不能包含原始聊天或工具输出。聊天内可使用 `/hm`、`/hm on`、`/hm off`、`/hm status` 控制注入；关闭不影响 daemon 和 watcher。规则安装或更新后需要重载 Pi 或新建任务。完成阶段性工作后使用：

```bash
python3 -m hippomind ingest --kind progress --agent pi --task "$TASK_ID" \
  --content "<可验证进度>" --evidence "task:$TASK_ID"
```

对于决定、阻塞、文件和下一步，分别使用 `decision`、`blocker`、`file`、`next_step`。Pi 不需要复制整段历史、维护自己的全量记忆文件，或要求用户输入任何记忆触发词。

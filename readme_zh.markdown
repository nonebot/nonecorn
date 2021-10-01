# Nonecorn
专为nonebot优化的asgi服务器


其前身是hypercorn。 

nonecorn保持同步上游 ，api兼容的同时，修复了很多bug，加入了很多新功能，
比如
- 专为nonebot设置的更大更快的ws缓冲区，方便以base64发送文件

- gunicorn集成
gunicorn --worker-class hypercorn.workers.HypercornUvloopWorker ，
可以“优雅的关闭”

- multiprocessing模块修复，避免出现在win上关不掉的问题 ~~uvicorn：啊嚏~~
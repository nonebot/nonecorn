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

- 新扩展 ```http.response.zerocopysend```

- 新扩展 ```http.trailingheaders.send``` 发送trailer header

- type Literal["http.trailingheaders.send"]
- headers Iterable[[byte string, byte string]]
- more_body bool 默认false

允许send receive event 里面的meta key
- headers Iterable[[byte string, byte string]] 就是trailer
- flush bool True则无视http/2的优先级规则直接发送 默认False
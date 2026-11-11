# -*- coding: utf-8 -*-
# 今日头条内容类型与字段常量

# 列表 tab 与接口 category 映射
CATEGORY_MAP = {
    "all": "profile_all",
    "article": "pc_profile_article",
    "video": "pc_profile_video",
    "weitoutiao": "pc_profile_weitoutiao",
}

# 内容类型中文名（按 cell_type / has_video 判定）
TYPE_ARTICLE = "文章"
TYPE_VIDEO = "视频"
TYPE_MICRO = "微头条"


# 响应拦截脚本：在页面任何 JS 之前注入，hook fetch 与 XHR，
# 让网站自己发带 a_bogus/_signature 签名的请求，我们只负责捕获响应 JSON。
CAPTURE_HOOK_JS = r"""
(function () {
  if (window.__TT_CAPTURED__) return;
  window.__TT_CAPTURED__ = { feed: [], comments: [], search: [] };

  function classify(url) {
    try {
      var u = String(url);
      if (u.indexOf('/api/pc/list/user/feed') > -1) return 'feed';
      if (u.indexOf('tab_comments') > -1) return 'comments';
      if (u.indexOf('/api/search') > -1 || u.indexOf('search/content') > -1) return 'search';
    } catch (e) {}
    return null;
  }

  function push(bucket, url, obj) {
    try {
      window.__TT_CAPTURED__[bucket].push({ url: String(url), data: obj, ts: Date.now() });
    } catch (e) {}
  }

  // hook fetch
  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function () {
      var args = arguments;
      var url = (args[0] && args[0].url) ? args[0].url : args[0];
      var bucket = classify(url);
      var p = origFetch.apply(this, args);
      if (bucket) {
        p.then(function (resp) {
          resp.clone().text().then(function (txt) {
            try { push(bucket, url, JSON.parse(txt)); } catch (e) {}
          });
        }).catch(function () {});
      }
      return p;
    };
  }

  // hook XMLHttpRequest
  var XHR = window.XMLHttpRequest;
  if (XHR && XHR.prototype) {
    var origOpen = XHR.prototype.open;
    var origSend = XHR.prototype.send;
    XHR.prototype.open = function (method, url) {
      this.__tt_url = url;
      this.__tt_bucket = classify(url);
      return origOpen.apply(this, arguments);
    };
    XHR.prototype.send = function () {
      var self = this;
      if (this.__tt_bucket) {
        this.addEventListener('load', function () {
          try { push(self.__tt_bucket, self.__tt_url, JSON.parse(self.responseText)); } catch (e) {}
        });
      }
      return origSend.apply(this, arguments);
    };
  }
})();
"""

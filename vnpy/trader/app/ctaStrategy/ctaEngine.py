# encoding: UTF-8

'''
本文件中实现了CTA策略引擎，针对CTA类型的策略，抽象简化了部分底层接口的功能。

关于平今和平昨规则：
1. 普通的平仓OFFSET_CLOSET等于平昨OFFSET_CLOSEYESTERDAY
2. 只有上期所的品种需要考虑平今和平昨的区别
3. 当上期所的期货有今仓时，调用Sell和Cover会使用OFFSET_CLOSETODAY，否则
   会使用OFFSET_CLOSE
4. 以上设计意味着如果Sell和Cover的数量超过今日持仓量时，会导致出错（即用户
   希望通过一个指令同时平今和平昨）
5. 采用以上设计的原因是考虑到vn.trader的用户主要是对TB、MC和金字塔类的平台
   感到功能不足的用户（即希望更高频的交易），交易策略不应该出现4中所述的情况
6. 对于想要实现4中所述情况的用户，需要实现一个策略信号引擎和交易委托引擎分开
   的定制化统结构（没错，得自己写）
'''



import json
import os
import traceback
from collections import OrderedDict,defaultdict
from datetime import datetime, timedelta
from copy import copy
from vnpy.event import Event
from vnpy.trader.vtEvent import *
from vnpy.trader.vtConstant import *
from vnpy.trader.vtObject import VtTickData, VtBarData
from vnpy.trader.vtGateway import VtSubscribeReq, VtOrderReq, VtCancelOrderReq, VtLogData
from vnpy.trader.vtFunction import todayDate, getJsonPath

from .ctaBase import *
from .strategy import STRATEGY_CLASS




########################################################################
class CtaEngine(object):
    """CTA策略引擎"""
    settingFileName = 'CTA_setting.json'
    settingfilePath = getJsonPath(settingFileName, __file__)

    STATUS_FINISHED = set([STATUS_REJECTED, STATUS_CANCELLED, STATUS_ALLTRADED])

    #----------------------------------------------------------------------
    def __init__(self, mainEngine, eventEngine):
        """Constructor"""
        self.mainEngine = mainEngine
        self.eventEngine = eventEngine

        # 当前日期
        self.today = todayDate()
        self.minute_temp = 0

        # 保存策略实例的字典
        # key为策略名称，value为策略实例，注意策略名称不允许重复
        self.strategyDict = {}

        # 保存vtSymbol和策略实例映射的字典（用于推送tick数据）
        # 由于可能多个strategy交易同一个vtSymbol，因此key为vtSymbol
        # value为包含所有相关strategy对象的list
        self.tickStrategyDict = {}

        # 保存vtOrderID和strategy对象映射的字典（用于推送order和trade数据）
        # key为vtOrderID，value为strategy对象
        self.orderStrategyDict = {}

        # 本地停止单编号计数
        self.stopOrderCount = 0
        # stopOrderID = STOPORDERPREFIX + str(stopOrderCount)

        # 本地停止单字典
        # key为stopOrderID，value为stopOrder对象
        self.stopOrderDict = {}             # 停止单撤销后不会从本字典中删除
        self.workingStopOrderDict = {}      # 停止单撤销后会从本字典中删除

        # 保存策略名称和委托号列表的字典
        # key为name，value为保存orderID（限价+本地停止）的集合
        self.strategyOrderDict = {}
        self.symbolList =  []
        # 成交号集合，用来过滤已经收到过的成交推送
        self.tradeSet = set()

        # 引擎类型为实盘
        self.engineType = ENGINETYPE_TRADING

        # 注册日式事件类型
        self.mainEngine.registerLogEvent(EVENT_CTA_LOG)

        # 注册事件监听
        self.registerEvent()

    #----------------------------------------------------------------------
    def sendOrder(self, vtSymbol, orderType, price, volume, marketPrice, strategy):
        """发单"""
        
        contract = self.mainEngine.getContract(vtSymbol)
        req = VtOrderReq()
        
        req.symbol = contract.symbol
        req.contractType = req.symbol[4:]
        req.exchange = contract.exchange
        req.vtSymbol = contract.vtSymbol
        req.price = self.roundToPriceTick(contract.priceTick, price)
        req.volume = volume

        req.productClass = strategy.productClass
        req.currency = strategy.currency
        req.bystrategy = strategy.name

        # 设计为CTA引擎发出的委托只允许使用限价单
        # req.priceType = PRICETYPE_LIMITPRICE

        req.priceType = marketPrice    #OKEX 用number作priceType

        if marketPrice:
            type_shown = PRICETYPE_MARKETPRICE
        else:
            type_shown = PRICETYPE_LIMITPRICE

        # CTA委托类型映射
        if orderType == CTAORDER_BUY:
            req.direction = DIRECTION_LONG
            req.offset = OFFSET_OPEN
            
        elif orderType == CTAORDER_SELL:
            req.direction = DIRECTION_SHORT
            req.offset = OFFSET_CLOSE
                
        elif orderType == CTAORDER_SHORT:
            req.direction = DIRECTION_SHORT
            req.offset = OFFSET_OPEN
            
        elif orderType == CTAORDER_COVER:
            req.direction = DIRECTION_LONG
            req.offset = OFFSET_CLOSE
        # 委托转换
        reqList = self.mainEngine.convertOrderReq(req)
        vtOrderIDList = []
        if not reqList:
            return vtOrderIDList
        for convertedReq in reqList:
            vtOrderID = self.mainEngine.sendOrder(convertedReq, contract.gatewayName)    # 发单
            self.orderStrategyDict[vtOrderID] = strategy                                 # 保存vtOrderID和策略的映射关系
            self.strategyOrderDict[strategy.name].add(vtOrderID)                         # 添加到策略委托号集合中
            vtOrderIDList.append(vtOrderID)
        self.writeCtaLog('策略%s: 发送%s委托%s, 交易：%s，%s，数量：%s @ %s'
                         %(strategy.name,type_shown,vtOrderID, vtSymbol, orderType, volume, price ))

        return vtOrderIDList

    #----------------------------------------------------------------------
    def cancelOrder(self, vtOrderID):
        """撤单"""
        # 查询报单对象
        order = self.mainEngine.getOrder(vtOrderID)

        # 如果查询成功
        if order:
            # 检查是否报单还有效，只有有效时才发出撤单指令
            orderFinished = (order.status==STATUS_ALLTRADED or order.status==STATUS_CANCELLED)
            if not orderFinished:
                req = VtCancelOrderReq()
                req.vtSymbol = order.vtSymbol
                req.symbol = order.symbol

                req.contractType = order.contractType
                req.exchange = order.exchange
                req.frontID = order.frontID
                req.sessionID = order.sessionID
                req.orderID = order.orderID
                self.mainEngine.cancelOrder(req, order.gatewayName)
                self.writeCtaLog('策略%s: 对本地订单%s，品种%s发送撤单委托'%(order.bystrategy, vtOrderID, order.vtSymbol))

    #----------------------------------------------------------------------
    def sendStopOrder(self, vtSymbol, orderType, price, volume, marketPrice, strategy):
        """发停止单（本地实现）"""
        self.stopOrderCount += 1
        stopOrderID = STOPORDERPREFIX + str(self.stopOrderCount)

        so = StopOrder()
        so.vtSymbol = vtSymbol
        contractType = vtSymbol.split('.')[0]
        so.contractType = contractType[4:]
        so.orderType = orderType
        so.price = price
        so.priceType = marketPrice
        so.volume = volume
        so.strategy = strategy
        so.stopOrderID = stopOrderID
        so.status = STOPORDER_WAITING

        if orderType == CTAORDER_BUY:
            so.direction = DIRECTION_LONG
            so.offset = OFFSET_OPEN
        elif orderType == CTAORDER_SELL:
            so.direction = DIRECTION_SHORT
            so.offset = OFFSET_CLOSE
        elif orderType == CTAORDER_SHORT:
            so.direction = DIRECTION_SHORT
            so.offset = OFFSET_OPEN
        elif orderType == CTAORDER_COVER:
            so.direction = DIRECTION_LONG
            so.offset = OFFSET_CLOSE

        # 保存stopOrder对象到字典中
        self.stopOrderDict[stopOrderID] = so
        self.workingStopOrderDict[stopOrderID] = so

        # 保存stopOrderID到策略委托号集合中
        self.strategyOrderDict[strategy.name].add(stopOrderID)

        # 推送停止单状态
        strategy.onStopOrder(so)

        return [stopOrderID]

    #----------------------------------------------------------------------
    def cancelStopOrder(self, stopOrderID):
        """撤销停止单"""
        # 检查停止单是否存在
        if stopOrderID in self.workingStopOrderDict:
            so = self.workingStopOrderDict[stopOrderID]
            strategy = so.strategy

            # 更改停止单状态为已撤销
            so.status = STOPORDER_CANCELLED

            # 从活动停止单字典中移除
            del self.workingStopOrderDict[stopOrderID]

            # 从策略委托号集合中移除
            s = self.strategyOrderDict[strategy.name]
            if stopOrderID in s:
                s.remove(stopOrderID)

            # 通知策略
            strategy.onStopOrder(so)

    #----------------------------------------------------------------------
    def processStopOrder(self, tick):
        """收到行情后处理本地停止单（检查是否要立即发出）"""
        vtSymbol = tick.vtSymbol

        # 首先检查是否有策略交易该合约
        if vtSymbol in self.tickStrategyDict:
            # 遍历等待中的停止单，检查是否会被触发
            for so in list(self.workingStopOrderDict.values()):
                if so.vtSymbol == vtSymbol:
                    longTriggered = so.direction==DIRECTION_LONG and tick.lastPrice>=so.price        # 多头停止单被触发
                    shortTriggered = so.direction==DIRECTION_SHORT and tick.lastPrice<=so.price     # 空头停止单被触发

                    if longTriggered or shortTriggered:
                        # 买入和卖出分别以涨停跌停价发单（模拟市价单）
                        if so.direction==DIRECTION_LONG:
                            price = tick.upperLimit
                        else:
                            price = tick.lowerLimit

                        # 发出市价委托
                        self.sendOrder(so.vtSymbol, so.orderType, price, so.volume, so.marketPrice, so.strategy)

                        # 从活动停止单字典中移除该停止单
                        del self.workingStopOrderDict[so.stopOrderID]

                        # 从策略委托号集合中移除
                        s = self.strategyOrderDict[strategy.name]
                        if so.stopOrderID in s:
                            s.remove(so.stopOrderID)

                        # 更新停止单状态，并通知策略
                        so.status = STOPORDER_TRIGGERED
                        so.strategy.onStopOrder(so)

    #----------------------------------------------------------------------
    def processTickEvent(self, event):
        """处理行情推送"""
        tick = event.dict_['data']
        # 收到tick行情后，先处理本地停止单（检查是否要立即发出）
        self.processStopOrder(tick)

        # 推送tick到对应的策略实例进行处理
        if tick.vtSymbol in self.tickStrategyDict:
             #tick时间可能出现异常数据，使用try...except实现捕捉和过滤
            try:
                # 添加datetime字段
                if not tick.datetime:
                    tick.datetime = datetime.strptime(' '.join([tick.date, tick.time]), '%Y%m%d %H:%M:%S.%f')
            except ValueError:
                self.writeCtaLog(traceback.format_exc())
                return

            # 逐个推送到策略实例中
            l = self.tickStrategyDict[tick.vtSymbol]
            for strategy in l:
                if strategy.trading:
                    self.callStrategyFunc(strategy, strategy.onTick, tick)
                    if tick.datetime.second == 40 and tick.datetime.minute != self.minute_temp:
                        self.minute_temp = tick.datetime.minute
                        self.qryOrder(strategy.name)

    #----------------------------------------------------------------------
    def processOrderEvent(self, event):
        """处理委托推送"""
        order = event.dict_['data']
        vtOrderID = order.vtOrderID
        if vtOrderID in self.orderStrategyDict:
            strategy = self.orderStrategyDict[vtOrderID]

            if 'eveningDict' in strategy.syncList:
                if order.status == STATUS_CANCELLED:
                    if order.direction == DIRECTION_LONG and order.offset == OFFSET_CLOSE:
                        posName = order.vtSymbol.replace(".","_") + "_SHORT"
                        strategy.eveningDict[posName] += order.totalVolume - order.tradedVolume
                    elif order.direction == DIRECTION_SHORT and order.offset == OFFSET_CLOSE:
                        posName = order.vtSymbol.replace(".","_") + "_LONG"
                        strategy.eveningDict[posName] += order.totalVolume - order.tradedVolume

                elif order.status == STATUS_ALLTRADED or order.status == STATUS_PARTTRADED:
                    if order.direction == DIRECTION_LONG and order.offset == OFFSET_OPEN:
                        posName = order.vtSymbol.replace(".","_") + "_LONG"
                        strategy.eveningDict[posName] += order.thisTradedVolume
                    elif order.direction == DIRECTION_SHORT and order.offset == OFFSET_OPEN:
                        posName = order.vtSymbol.replace(".","_") + "_SHORT"
                        strategy.eveningDict[posName] += order.thisTradedVolume
                        
                elif order.status == STATUS_NOTTRADED:
                    if order.direction == DIRECTION_LONG and order.offset == OFFSET_CLOSE:
                        posName = order.vtSymbol.replace(".","_") + "_SHORT"
                        strategy.eveningDict[posName] -= order.totalVolume
                    elif order.direction == DIRECTION_SHORT and order.offset == OFFSET_CLOSE:
                        posName = order.vtSymbol.replace(".","_") + "_LONG"
                        strategy.eveningDict[posName] -= order.totalVolume
                

            # 如果委托已经完成（拒单、撤销、全成），则从活动委托集合中移除
            if order.status in self.STATUS_FINISHED:
                s = self.strategyOrderDict[strategy.name]
                if vtOrderID in s:
                    s.remove(vtOrderID)

            self.callStrategyFunc(strategy, strategy.onOrder, order)
            self.saveOrderDetail(strategy,order)

    #----------------------------------------------------------------------
    def processTradeEvent(self, event):
        """处理成交推送"""
        trade = event.dict_['data']
        # 过滤已经收到过的成交回报
        if trade.vtTradeID in self.tradeSet:
            return
        self.tradeSet.add(trade.vtTradeID)
        # 将成交推送到策略对象中
        if trade.vtOrderID in self.orderStrategyDict:
            strategy = self.orderStrategyDict[trade.vtOrderID]

            # 计算策略持仓
            # if trade.direction == DIRECTION_LONG and trade.offset == OFFSET_OPEN:
            #     posName = trade.vtSymbol.replace(".","_") + "_LONG"
            #     strategy.posDict[str(posName)] += trade.volume
            # elif trade.direction == DIRECTION_LONG and trade.offset == OFFSET_CLOSE:
            #     posName = trade.vtSymbol.replace(".","_") + "_SHORT"
            #     strategy.posDict[str(posName)] -= trade.volume
            # elif trade.direction ==DIRECTION_SHORT and trade.offset == OFFSET_CLOSE:
            #     posName = trade.vtSymbol.replace(".","_") + "_LONG"
            #     strategy.posDict[str(posName)] -= trade.volume
            # elif trade.direction ==DIRECTION_SHORT and trade.offset == OFFSET_OPEN:
            #     posName = trade.vtSymbol.replace(".","_") + "_SHORT"
            #     strategy.posDict[str(posName)] += trade.volume
            # elif trade.direction == DIRECTION_LONG and trade.offset == OFFSET_NONE:
            #     posName = trade.vtSymbol.replace(".","_")
            #     strategy.posDict[str(posName)] += trade.volume
            # elif trade.direction ==DIRECTION_SHORT and trade.offset == OFFSET_NONE:
            #     posName = trade.vtSymbol.replace(".","_")
            #     strategy.posDict[str(posName)] -= trade.volume

            self.callStrategyFunc(strategy, strategy.onTrade, trade)
    #----------------------------------
    def processPositionEvent(self, event):
        """处理持仓推送"""
        pos = event.dict_['data']

        symbol = pos.vtSymbol
        if 'quarter' in symbol or 'week' in symbol or 'bitmex' in symbol:
            productType = 'FUTURE'
        else:
            productType = 'SPOT'

        for strategy in self.strategyDict.values():
            if strategy.inited and pos.vtSymbol in strategy.symbolList:
                if productType == 'FUTURE':
                    if pos.direction == DIRECTION_LONG:
                        posName = pos.vtSymbol.replace(".","_") + "_LONG"
                        if 'posDict' in strategy.syncList:
                            strategy.posDict[str(posName)] = pos.position
                        if 'eveningDict' in strategy.syncList:
                            strategy.eveningDict[str(posName)] = pos.position - pos.frozen
                        if 'bondDict' in strategy.syncList:
                            strategy.bondDict[str(posName)]=pos.frozen
                    elif pos.direction == DIRECTION_SHORT:
                        posName2 = pos.vtSymbol.replace(".","_") + "_SHORT"
                        if 'posDict' in strategy.syncList:
                            strategy.posDict[str(posName2)] = pos.position
                        if 'eveningDict' in strategy.syncList:
                            strategy.eveningDict[str(posName2)] = pos.position - pos.frozen
                        if 'bondDict' in strategy.syncList:
                            strategy.bondDict[str(posName2)]=pos.frozen
                elif productType == 'SPOT':
                    posName = pos.vtSymbol.replace(".","_")
                    if 'posDict' in strategy.syncList:
                        strategy.posDict[str(posName)] = pos.position
                    if 'bondDict' in strategy.syncList:
                        strategy.bondDict[str(posName)] = pos.frozen

                # 保存策略持仓到数据库
                self.saveSyncData(strategy)  

    #------------------------------------------------------
    def processAccountEvent(self,event):
        """账户推送"""
        account = event.dict_['data']

        for strategy in self.strategyDict.values():
            if strategy.inited:
                accountName = account.vtSymbol.replace('.','_')
                if 'accountDict' in strategy.syncList:
                    strategy.accountDict[str(posName)] = account.position
                if 'frozenDict' in strategy.syncList:
                    strategy.bondDict[str(posName)] = account.frozen

    #--------------------------------------------------
    def registerEvent(self):
        """注册事件监听"""
        self.eventEngine.register(EVENT_TICK, self.processTickEvent)
        self.eventEngine.register(EVENT_POSITION, self.processPositionEvent)
        self.eventEngine.register(EVENT_ORDER, self.processOrderEvent)
        self.eventEngine.register(EVENT_TRADE, self.processTradeEvent)
        self.eventEngine.register(EVENT_ACCOUNT, self.processAccountEvent)



    #----------------------------------------------------------------------
    def insertData(self, dbName, collectionName, data):
        """插入数据到数据库（这里的data可以是VtTickData或者VtBarData）"""
        for collectionName_ in collectionName:
            self.mainEngine.dbInsert(dbName, collectionName_, data.__dict__)

    #----------------------------------------------------------------------
    def loadBar(self, dbName, collectionName, days):
        """从数据库中读取Bar数据，startDate是datetime对象"""
        startDate = self.today - timedelta(days)
        for collectionName_ in collectionName:
            d = {'datetime':{'$gte':startDate}}
            
            barData = self.mainEngine.dbQuery(dbName, collectionName_, d, 'datetime')

            l = []
            for d in barData:
                bar = VtBarData()
                bar.__dict__ = d
                bar.vtSymbol = collectionName_
                l.append(bar)
            return l

    #----------------------------------------------------------------------
    def loadTick(self, dbName, collectionName, days):
        """从数据库中读取Tick数据，startDate是datetime对象"""
        startDate = self.today - timedelta(days)
        for collectionName_ in collectionName:

            d = {'datetime':{'$gte':startDate}}
            tickData = self.mainEngine.dbQuery(dbName, collectionName_, d, 'datetime')

            l = []
            for d in tickData:
                tick = VtTickData()
                tick.__dict__ = d
                l.append(tick)
            return l

    #----------------------------------------------------------------------
    def writeCtaLog(self, content):
        """快速发出CTA模块日志事件"""
        log = VtLogData()
        log.logContent = content
        log.gatewayName = 'CTA_STRATEGY'
        event = Event(type_=EVENT_CTA_LOG)
        event.dict_['data'] = log
        self.eventEngine.put(event)

    #----------------------------------------------------------------------
    def loadStrategy(self, setting):
        """载入策略"""
        try:
            name = setting['name']
            className = setting['className']
            vtSymbolset=setting['vtSymbol']
            vtSymbolList=vtSymbolset.split(',')

        except Exception as e:
            self.writeCtaLog('载入策略出错：%s' %e)
            return

        # 获取策略类
        strategyClass = STRATEGY_CLASS.get(className, None)
        if not strategyClass:
            self.writeCtaLog('找不到策略类：%s' %className)
            return

        # 防止策略重名
        if name in self.strategyDict:
            self.writeCtaLog('策略实例重名：%s' %name)
        else:
            # 创建策略实例
            strategy = strategyClass(self, setting)
            self.strategyDict[name] = strategy

            # 创建委托号列表
            self.strategyOrderDict[name] = set()
            for vtSymbol in vtSymbolList :

                # 保存Tick映射关系
                if vtSymbol in self.tickStrategyDict:
                    l = self.tickStrategyDict[vtSymbol]
                else:
                    l = []
                    self.tickStrategyDict[vtSymbol] = l
                l.append(strategy)

    #-----------------------------------------------------------------------
    def subscribeMarketData(self, strategy):
        """订阅行情"""
        # 订阅合约
        for vtSymbol in strategy.symbolList:
            contract = self.mainEngine.getContract(vtSymbol)
            if contract:
                req = VtSubscribeReq()
                req.symbol = contract.symbol
                req.vtSymbol = contract.vtSymbol
                req.exchange = contract.exchange

                if contract.contractType:         # 如果该品种是OKEX期货
                    req.contractType = contract.contractType
                
                # 对于IB接口订阅行情时所需的货币和产品类型，从策略属性中获取
                req.currency = strategy.currency
                req.productClass = strategy.productClass
                
                self.mainEngine.subscribe(req, contract.gatewayName)
            else:
                self.writeCtaLog(u'策略%s的交易合约%s无法找到' %(strategy.name, vtSymbol))

    #----------------------------------------------------------------------
    def initStrategy(self, name):
        """初始化策略"""
        if name in self.strategyDict:
            strategy = self.strategyDict[name]

            if not strategy.inited:
                strategy.inited = True
                self.callStrategyFunc(strategy, strategy.onInit)
                self.subscribeMarketData(strategy)                      # 加载同步数据后再订阅行情
                self.initPosition(strategy)


            else:
                self.writeCtaLog('请勿重复初始化策略实例：%s' %name)
        else:
            self.writeCtaLog('策略实例不存在：%s' %name)

    #---------------------------------------------------------------------
    def startStrategy(self, name):
        """启动策略"""
        if name in self.strategyDict:
            strategy = self.strategyDict[name]

            if strategy.inited and not strategy.trading:
                strategy.trading = True
                self.callStrategyFunc(strategy, strategy.onStart)
                # self.loadSyncData(strategy)            # 初始化完成后加载同步数据
        else:
            self.writeCtaLog('策略实例不存在：%s' %name)

    #----------------------------------------------------------------------
    def stopStrategy(self, name):
        """停止策略"""
        if name in self.strategyDict:
            strategy = self.strategyDict[name]

            if strategy.trading:
                strategy.trading = False
                self.callStrategyFunc(strategy, strategy.onStop)

                # 对该策略发出的所有限价单进行撤单
                for vtOrderID, s in list(self.orderStrategyDict.items()):
                    if s is strategy:
                        self.cancelOrder(vtOrderID)

                # 对该策略发出的所有本地停止单撤单
                for stopOrderID, so in list(self.workingStopOrderDict.items()):
                    if so.strategy is strategy:
                        self.cancelStopOrder(stopOrderID)

            strategy.inited = False  ## 取消注释使策略在停止后可以再次初始化
            ## 加上删除持仓信息
        else:
            self.writeCtaLog('策略实例不存在：%s' %name)

    #----------------------------------------------------------------------
    def initAll(self):
        """全部初始化"""
        for name in list(self.strategyDict.keys()):
            self.initStrategy(name)

    #----------------------------------------------------------------------
    def startAll(self):
        """全部启动"""
        for name in list(self.strategyDict.keys()):
            self.startStrategy(name)

    #----------------------------------------------------------------------
    def stopAll(self):
        """全部停止"""
        for name in list(self.strategyDict.keys()):
            self.stopStrategy(name)

    #----------------------------------------------------------------------
    def saveSetting(self):
        """保存策略配置"""
        with open(self.settingfilePath, 'w') as f:
            l = []

            for strategy in list(self.strategyDict.values()):
                setting = {}
                for param in strategy.paramList:
                    setting[param] = strategy.__getattribute__(param)
                l.append(setting)

            jsonL = json.dumps(l, indent=4)
            f.write(jsonL)

    #----------------------------------------------------------------------
    def loadSetting(self):
        """读取策略配置"""
        with open(self.settingfilePath) as f:
            l = json.load(f)

            for setting in l:
                self.loadStrategy(setting)

        # for strategy in self.strategyDict.values():
        #     self.loadSyncData(strategy)

    #----------------------------------------------------------------------
    def getStrategyVar(self, name):
        """获取策略当前的变量字典"""
        if name in self.strategyDict:
            strategy = self.strategyDict[name]
            varDict = OrderedDict()

            for key in strategy.varList:
                varDict[key] = strategy.__getattribute__(key)

            return varDict
        else:
            self.writeCtaLog('策略实例不存在：' + name)
            return None

    #----------------------------------------------------------------------
    def getStrategyParam(self, name):
        """获取策略的参数字典"""
        if name in self.strategyDict:
            strategy = self.strategyDict[name]
            paramDict = OrderedDict()

            for key in strategy.paramList:
                paramDict[key] = strategy.__getattribute__(key)

            return paramDict
        else:
            self.writeCtaLog('策略实例不存在：' + name)
            return None

    #----------------------------------------------------------------------
    def putStrategyEvent(self, name):
        """触发策略状态变化事件（通常用于通知GUI更新）"""
        event = Event(EVENT_CTA_STRATEGY+name)
        self.eventEngine.put(event)

    #----------------------------------------------------------------------
    def callStrategyFunc(self, strategy, func, params=None):
        """调用策略的函数，若触发异常则捕捉"""
        try:
            if params:
                func(params)
            else:
                func()
        except Exception:
            # 停止策略，修改状态为未初始化
            strategy.trading = False
            strategy.inited = False
            self.saveSyncData(strategy)
            self.saveVarData(strategy)

            # 发出日志
            content = '\n'.join(['策略%s：触发异常已停止, 当前状态已保存' %strategy.name,
                                traceback.format_exc()])
            self.writeCtaLog(content)

    #----------------------------------------------------------------------------------------
    def saveSyncData(self, strategy):    #改为posDict
        """保存策略的持仓情况到数据库"""

        flt = {'name': strategy.name,
            'posName':str(strategy.symbolList)}
        
        d = copy(flt)
        for key in strategy.syncList:
            d[key] = strategy.__getattribute__(key)

        self.mainEngine.dbUpdate(POSITION_DB_NAME, strategy.name,
                                    d, flt, True)
                
        content = u'策略%s: 同步数据保存成功\n当前持仓%s\n可平仓量%s\n保证金%s' %(strategy.name, strategy.posDict,strategy.eveningDict,strategy.bondDict)
        self.writeCtaLog(content)


    def saveVarData(self, strategy):
        flt = {'name': strategy.name,
            'posName':str(strategy.symbolList)}
        
        d = copy(flt)
        for key in strategy.varList:
            d[key] = strategy.__getattribute__(key)

        self.mainEngine.dbUpdate(VAR_DB_NAME, strategy.name,
                                    d, flt, True)
                
        content = u'策略%s: 参数数据保存成功' %(strategy.name)
        self.writeCtaLog(content)

    #----------------------------------------------------------------------
    def loadSyncData(self, strategy):
        """从数据库载入策略的持仓情况"""

        flt = {'name': strategy.name,
        'posName': str(strategy.symbolList)}
        syncData = self.mainEngine.dbQuery(POSITION_DB_NAME, strategy.name, flt)
        
        if not syncData:
            self.writeCtaLog(u'策略%s: 当前没有持仓信息'%strategy.name)
            return
        
        d = syncData[0]
        for key in strategy.syncList:
            if key in d:
                strategy.__setattr__(key, d[key])

    def loadVarData(self, strategy):
        """从数据库载入策略的持仓情况"""

        flt = {'name': strategy.name,
        'posName': str(strategy.symbolList)}
        varData = self.mainEngine.dbQuery(VAR_DB_NAME, strategy.name, flt)
        
        if not varData:
            self.writeCtaLog(u'策略%s: 当前没有持仓信息'%strategy.name)
            return
        
        d = varData[0]
        for key in strategy.varList:
            if key in d:
                strategy.__setattr__(key, d[key])

    def saveOrderDetail(self, strategy, order):
        """
        将订单信息存入数据库
        """
        flt = {'name': strategy.name,
            'vtOrderID':order.vtOrderID,
            'symbol':order.vtSymbol,
            'exchageID': order.exchangeOrderID,
            'direction':order.direction,
            'offset':order.offset,
            'price': order.price,
            'price_avg': order.price_avg,
            'tradedVolume':order.tradedVolume,
            'totalVolume':order.totalVolume,
            'status':order.status,
            'createTime':order.orderTime,
            'orderby':order.bystrategy
            }

        self.mainEngine.dbInsert(ORDER_DB_NAME, strategy.name, flt)
        content = u'策略%s: 保存%s订单数据成功，本地订单号%s' %(strategy.name, order.vtSymbol, order.vtOrderID)
        self.writeCtaLog(content)
        
    #----------------------------------------------------------------------
    def roundToPriceTick(self, priceTick, price):
        """取整价格到合约最小价格变动"""
        if not priceTick:
            return price

        newPrice = round(price/priceTick, 0) * priceTick
        return newPrice

    #----------------------------------------------------------------------
    def stop(self):
        """停止"""
        pass

    #----------------------------------------------------------------------
    def cancelAll(self, name):
        """全部撤单"""
        s = self.strategyOrderDict[name]

        # 遍历列表，查找非停止单全部撤单
        # 这里不能直接遍历集合s，因为撤单时会修改s中的内容，导致出错
        for orderID in list(s):
            if STOPORDERPREFIX not in orderID:
                self.cancelOrder(orderID)

    def cancelAllStopOrder(self,name):
        """撤销所有停止单"""
        s= self.strategyOrderDict[name]
        for orderID in list(s):
            if STOPORDERPREFIX in orderID:
                self.cancelStopOrder(orderID)

    #----------------------------------------------------------------------
    def getPriceTick(self, strategy):
        """获取最小价格变动"""

        for vtSymbol in strategy.symbolList:
            contract = self.mainEngine.getContract(vtSymbol)
            if contract:
                return contract.priceTick
            return 0

    #--------------------------------------------------------------
    def loadHistoryBar(self,vtSymbol,type_,size = None,since = None):
        """读取历史数据"""
        data = self.mainEngine.loadHistoryBar(vtSymbol, type_, size, since)
        histbar = []
        for i in range(len(data['open'])):
            bar = VtBarData()
            bar.open = data['open'][i]
            bar.close = data['close'][i]
            bar.high = data['high'][i]
            bar.low = data['low'][i]
            bar.volume = data['volume'][i]
            bar.vtSymbol = vtSymbol
            bar.datetime = data['datetime'][i]
            histbar.append(bar)

        return histbar

    def initPosition(self,strategy):
        # 因交易所更改仓位的读取方式，暂时取消使用defaultdict初始化posdict
        # for item in strategy.syncList:
        #     d = strategy.__getattribute__(item)
        #     d = defaultdict(None)
        
        # 该方法初始化仓位信息比较复杂，但可以不受交易所影响
        for i in range(len(strategy.symbolList)):
            symbol = strategy.symbolList[i]
            if 'week' in  symbol or 'quarter' in symbol or 'bitmex' in symbol:
                if 'posDict' in strategy.syncList:
                    strategy.posDict[symbol.replace(".","_")+"_LONG"] = 0
                    strategy.posDict[symbol.replace(".","_")+"_SHORT"] = 0
                if 'eveningDict' in strategy.syncList:
                    strategy.eveningDict[symbol.replace(".","_")+"_LONG"] = 0
                    strategy.eveningDict[symbol.replace(".","_")+"_SHORT"] = 0
                if 'bondDict' in strategy.syncList:
                    strategy.bondDict[symbol.replace(".","_")+"_LONG"] = 0
                    strategy.bondDict[symbol.replace(".","_")+"_SHORT"] = 0
            else:
                if 'accountDict' in strategy.syncList:
                    strategy.accountDict[symbol.replace(".","_")] = 0
                if 'posDict' in strategy.syncList:
                    strategy.posDict[symbol.replace(".","_")] = 0

        # 根据策略的品种信息，查询特定交易所该品种的持仓
        for vtSymbol in strategy.symbolList:
            contract = self.mainEngine.getContract(vtSymbol)
            self.mainEngine.initPosition(vtSymbol, contract.gatewayName)

    def qryOrder(self,name,status=None):
        s = self.strategyOrderDict[name]
        for vtOrderID in list(s):
            if STOPORDERPREFIX not in vtOrderID:
                order = self.mainEngine.getOrder(vtOrderID)
                self.mainEngine.qryOrder(order.vtSymbol, order.exchangeOrderID, status = None)

    def restoreStrategy(self, name):
        """恢复策略"""
        if name in self.strategyDict:
            strategy = self.strategyDict[name]

            if not strategy.inited and not strategy.trading:
                strategy.inited = True
                strategy.trading = True
                self.callStrategyFunc(strategy, strategy.onRestore)
                self.loadVarData(strategy)            # 初始化完成后加载同步数据
        else:
            self.writeCtaLog('策略实例不存在：%s' %name)
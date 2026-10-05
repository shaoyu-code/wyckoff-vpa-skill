"""Native drawing policy and bounded label placement for verified chart viewports.

Pure geometry does not certify TradingView rendering. Viewport inputs must come from
the actual chart. Screen coordinates are temporary layout data, never event identities.
No changes are made to event anchors, interpretation, candles, or manual annotations.
"""
from __future__ import annotations
from copy import deepcopy
import math
from .tradingview import NativeError
from ..contracts import digest

VERSION = 'tv11-layout-1'
BOOL_KEYS = ('seconds', 'minutes', 'hours', 'days', 'weeks', 'months', 'ticks', 'ranges')
SUPPORTED = {'1M': ('months', 1), '1W': ('weeks', 1), '1D': ('days', 1),
             '12H': ('hours', 12), '4H': ('hours', 4), '1H': ('hours', 1)}


def visibility(timeframe: str | None) -> dict:
    value = {name: False for name in BOOL_KEYS}
    # Definite finite ranges avoid inheriting native "all intervals" defaults.
    for unit in ('seconds', 'minutes', 'hours', 'days', 'weeks', 'months'):
        value[unit+'From'] = 1
        value[unit+'To'] = 1
    if timeframe is not None:
        if timeframe not in SUPPORTED:
            raise NativeError('UNSUPPORTED_VISIBILITY_TIMEFRAME')
        unit, count = SUPPORTED[timeframe]
        value.update({unit: True, unit+'From': count, unit+'To': count})
    return value


def validate_visibility(item: dict, timeframe: str) -> None:
    expected = visibility(None if item.get('retired') else timeframe)
    actual = item.get('overrides', {}).get('intervalsVisibilities')
    if actual != expected or any(type(actual.get(k)) is not bool for k in BOOL_KEYS):
        raise NativeError('NATIVE_TIMEFRAME_VISIBILITY_LEAK')


def rect_intersects(a, b, padding=0.0):
    return not (a[0]+a[2]+padding <= b[0] or b[0]+b[2]+padding <= a[0]
                or a[1]+a[3]+padding <= b[1] or b[1]+b[3]+padding <= a[1])


def validate_view(view: dict):
    required = {'id','product_id','timeframe','data_cutoff','time_x','price_low','price_high',
                'price_y_top','price_y_bottom','work_rect','obstacles','scale','text_sizes'}
    if not required <= set(view) or view['scale'] not in {'linear','log'}:
        raise NativeError('ACTUAL_VIEWPORT_PROFILE_REQUIRED')
    values = [view['price_low'],view['price_high'],view['price_y_top'],view['price_y_bottom'],
              *view['work_rect']]
    if any(type(x) not in (int,float) or not math.isfinite(x) for x in values):
        raise NativeError('NONFINITE_VIEWPORT')
    if view['price_high'] <= view['price_low'] or view['price_y_bottom'] <= view['price_y_top']:
        raise NativeError('INVALID_PRICE_PROJECTION')
    if view['scale'] == 'log' and view['price_low'] <= 0:
        raise NativeError('LOG_NONPOSITIVE_PRICE')
    if view['work_rect'][2] <= 0 or view['work_rect'][3] <= 0:
        raise NativeError('EMPTY_WORK_RECT')
    grid = sorted((int(t),float(x)) for t,x in view['time_x'].items())
    if len(grid) < 2 or any(not math.isfinite(x) for _,x in grid) or any(a[1] >= b[1] for a,b in zip(grid,grid[1:])):
        raise NativeError('ACTUAL_TIME_GRID_REQUIRED')
    if any(not isinstance(box,(list,tuple)) or len(box)!=4 or box[2]<0 or box[3]<0
           for box in view['obstacles']):
        raise NativeError('INVALID_OBSTACLE_RECTANGLE')
    if any(not isinstance(size,(list,tuple)) or len(size)!=2 or size[0]<=0 or size[1]<=0
           for size in view['text_sizes'].values()):
        raise NativeError('INVALID_NATIVE_TEXT_SIZE')
    for box in list(view['obstacles']) + list(view['text_sizes'].values()):
        if any(type(x) not in (int,float) or not math.isfinite(x) for x in box):
            raise NativeError('NONFINITE_GEOMETRY')
    return grid


def project(point, view):
    grid = dict(validate_view(view))
    if point['time'] not in grid:
        raise NativeError('LABEL_TIME_OUTSIDE_MEASURED_GRID')
    price = point['price']
    low, high = view['price_low'],view['price_high']
    if view['scale'] == 'log':
        if price <= 0: raise NativeError('LOG_NONPOSITIVE_LABEL')
        price,low,high = math.log(price),math.log(low),math.log(high)
    fraction = (price-low)/(high-low)
    y = view['price_y_bottom']-fraction*(view['price_y_bottom']-view['price_y_top'])
    return grid[point['time']],y


def inverse_price(y, view):
    fraction=(view['price_y_bottom']-y)/(view['price_y_bottom']-view['price_y_top'])
    low,high=view['price_low'],view['price_high']
    if view['scale']=='log': return math.exp(math.log(low)+fraction*(math.log(high)-math.log(low)))
    return low+fraction*(high-low)


def inspect_layout(items: list[dict], view: dict) -> dict:
    """Measured rectangles only. Caller supplies native text bbox convention."""
    validate_view(view)
    boxes, problems = {}, []
    wx,wy,ww,wh=view['work_rect']
    for item in items:
        if item['shape'] != 'text' or item.get('retired'): continue
        key=item['marker']
        if key not in view['text_sizes']:
            problems.append([key,'TEXT_SIZE_NOT_MEASURED']);continue
        width,height=view['text_sizes'][key]
        if width<=0 or height<=0:
            problems.append([key,'TEXT_SIZE_INVALID']);continue
        try: x,y=project(item['points'][0],view)
        except NativeError:
            problems.append([key,'OUTSIDE_MEASURED_TIME_GRID']);continue
        box=(x,y,width,height);boxes[key]=box
        if x<wx or y<wy or x+width>wx+ww or y+height>wy+wh:
            problems.append([key,'CLIPPED'])
        if any(rect_intersects(box,o) for o in view['obstacles']):
            problems.append([key,'CANDLE_VOLUME_OR_MANUAL_OVERLAP'])
    keys=list(boxes)
    for i,a in enumerate(keys):
        for b in keys[i+1:]:
            if rect_intersects(boxes[a],boxes[b],padding=4):
                problems.append([a,b,'LABEL_OVERLAP'])
    return {'view_id':view['id'],'passed':not problems,'problems':problems,
            'bbox_basis':view.get('bbox_basis','UNKNOWN'),
            'scope':'SUPPLIED_VIEWPORT_GEOMETRY_ONLY_NOT_NATIVE_VISUAL_CERTIFICATION'}


def arrange_labels(manifest: dict, view: dict) -> dict:
    """Place labels on actual time grid; links back to original, unchanged anchors.

    A deterministic bounded search, not a guarantee of arbitrary-zoom clarity.
    Missing measurements or an unsatisfiable layout raise instead of hiding evidence.
    """
    grid=validate_view(view)
    if any(manifest.get(k)!=view.get(k) for k in ('product_id','timeframe','data_cutoff')):
        raise NativeError('VIEWPORT_IDENTITY_MISMATCH')
    if view.get('bbox_basis') != 'TOP_LEFT_NATIVE_MEASURED':
        raise NativeError('NATIVE_TEXT_METRICS_REQUIRED')
    result=deepcopy(manifest);placed=[];links=[];mapping={}
    wx,wy,ww,wh=view['work_rect']
    # Mandatory information has deterministic precedence, not the newest timestamp alone.
    labels=[x for x in result['items'] if x['shape']=='text' and not x.get('retired')]
    labels.sort(key=lambda x:(x.get('display_priority',1),x['marker']))
    for item in labels:
        key=item['marker']
        if key not in view['text_sizes']:raise NativeError('NATIVE_TEXT_METRICS_REQUIRED')
        width,height=view['text_sizes'][key]
        if width<=0 or height<=0:raise NativeError('TEXT_SIZE_INVALID')
        anchor=deepcopy(item['points'][0]);ax,ay=project(anchor,view)
        # A drawing's semantic point is stored separately before the text is moved.
        choices=sorted(grid,key=lambda tx:(abs(tx[1]-ax),tx[0]))[:30]
        found=None
        for offset in (0,-1,1,-2,2,-3,3,-4,4):
            y=ay+offset*(height+6)
            for t,x in choices:
                box=(x,y,width,height)
                if x<wx or y<wy or x+width>wx+ww or y+height>wy+wh:continue
                if any(rect_intersects(box,o,padding=4) for o in [*view['obstacles'],*placed]):continue
                found=(t,x,y,box);break
            if found:break
        if found is None:raise NativeError('LAYOUT_UNSATISFIABLE_KEEP_EVIDENCE')
        t,x,y,box=found;display={'time':t,'price':inverse_price(y,view)}
        placed.append(box);mapping[key]={'anchor':anchor,'display':display}
        if display!=anchor:
            tag='[GMI:'+digest([key,'label-leader'])[:24]+']'
            links.append({'marker':tag,'shape':'trend_line','points':[anchor,display],
                'text':'标签引导，非趋势预测\n'+tag,
                'overrides':{'linecolor':'#777777','linewidth':1,
                             'intervalsVisibilities':visibility(manifest['timeframe'])},
                'display_role':'LABEL_LEADER','parent_marker':key})
        item['semantic_anchor']=anchor;item['points']=[display]
    result['items'].extend(links)
    result['label_mapping']=mapping
    result['layout_version']=VERSION
    result['layout_view_id']=view['id']
    result['layout_geometry']=inspect_layout(result['items'],view)
    if not result['layout_geometry']['passed']:
        raise NativeError('POST_LAYOUT_GEOMETRY_INVALID')
    result['visual_acceptance']='NATIVE_SCREENSHOT_AND_CROSS_VIEWPORT_NOT_RUN'
    return result



<div class="sectionbar">
    <form class="navbar-form">
        <table width="100%">
            <tr>
                <td>
                    <table>
                        <tr>
                            <td><span data-toggle="collapse" data-target="#world_editor"><i
                                    class="icon-chevron-right"></i></span></td>

                             <td>
                                <div class="btn-group" id="world_list">
                                    %include nodenet_list type="world",mine=mine,others=others,current=current
                                </div>
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </form>
</div>

<div id="world_editor" class="frontend_section section-margin collapse in">
    <div class="world section">
        %if world_assets.get('template'):
            %include(world_assets['template'], assets=world_assets)
        %else:
            <div class="editor_field">
                <canvas id="world" width="100%" height="500" style="background:#eeeeee; width:100%"></canvas>
            </div>
        %end
    </div>
    <div class="seperator" style="text-align:center;"><a class="resizeHandle" id="worldSizeHandle"> </a></div>
</div>

<div class="dropdown" id="create_object_menu">
    <a class="dropdown-toggle" data-toggle="dropdown" href="#create_object_menu"></a>
    <ul class="world_menu dropdown-menu">
        <li><a href="#" data="add_worldobject">Add worldobject</a></li>
    </ul>
</div>


<script src="/static/js/world.js" type="text/paperscript" canvas="world"></script>

%if world_assets.get('js'):
    <script src="/static/{{world_assets['js']}}" type="text/paperscript" canvas="world"></script>
%end
